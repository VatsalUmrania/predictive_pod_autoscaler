# operator/main.py — kopf timer handler (multi-CR orchestrator)
"""PPA Operator: manages N PredictiveAutoscaler CRs independently.

This file is a thin delegation layer:
- Startup hooks
- Health endpoint
- Prometheus metrics
- CR reconciliation → delegates to ScalerStateMachine
"""

import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import kopf
from prometheus_client import start_http_server as _prom_start_http_server

from ppa.config import DEFAULT_MODEL_DIR, INITIAL_DELAY, NAMESPACE, TIMER_INTERVAL
from ppa.domain import CRState

# Re-export metrics for backward compatibility and external access
from ppa.operator.model_bundle import (
    BundlePendingError,
    BundleValidationError,
    resolve_model_bundle,
)
from ppa.operator.state_machine import ScalerStateMachine

# Logging setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("ppa.operator")
logging.getLogger("kopf.objects").setLevel(logging.ERROR)
# Health endpoint
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)

    def log_message(self, format, *args):
        pass


def _start_health_server(port: int = 8080):
    server = HTTPServer(("", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Health endpoint listening on :{port}/healthz")


# Start health server and Prometheus metrics
_start_health_server()
_prom_start_http_server(9100)
logger.info("Prometheus metrics endpoint listening on :9100/metrics")
# Per-CR state registry
_cr_state: dict[tuple[str, str], CRState] = {}
_cr_state_lock = threading.Lock()

# Model bundle resolution
def _parse_crd_spec(
    spec: dict,
    status: dict | None,
    meta: dict,
    cr_ns: str,
    cr_name: str,
    patch: kopf.Patch,
) -> tuple[dict[str, Any], CRState]:
    """Parse CRD spec into config dict and load/create CR state."""

    from ppa.config import (
        DEFAULT_CAPACITY_PER_POD,
        DEFAULT_MIN_REPLICAS,
        DEFAULT_SCALE_DOWN_RATE,
        DEFAULT_SCALE_UP_RATE,
    )

    target = spec["targetDeployment"]
    target_ns = spec.get("namespace", cr_ns)
    target_app = spec.get("appName", target)
    target_horizon = spec.get("horizon", "rps_t3m")

    max_r = spec.get("maxReplicas")
    if max_r is None:
        raise ValueError("maxReplicas must be set in PredictiveAutoscaler spec")

    config = {
        "target": target,
        "target_ns": target_ns,
        "target_app": target_app,
        "target_horizon": target_horizon,
        "min_r": spec.get("minReplicas", DEFAULT_MIN_REPLICAS),
        "max_r": max_r,
        "capacity": spec.get("capacityPerPod", DEFAULT_CAPACITY_PER_POD),
        "up_rate": spec.get("scaleUpRate", DEFAULT_SCALE_UP_RATE),
        "down_rate": spec.get("scaleDownRate", DEFAULT_SCALE_DOWN_RATE),
        "safety_factor": float(spec.get("safetyFactor", 1.10)),
        "observer_mode": bool(spec.get("observerMode", False)),
        "container_name": spec.get("containerName") or None,
        "prom_url": spec.get("prometheusUrl") or None,
    }

    # Get or create state
    key = (cr_ns, cr_name)
    with _cr_state_lock:
        existing = _cr_state.get(key)

        if existing is None:
            existing = CRState(
                predictor=None,
                observer_mode=False,
                stable_count=0,
                last_prediction=0.0,
                last_desired=-1.0,
                last_known_good_replicas=0,
                last_known_good_prediction=0.0,
                consecutive_failures=0,
                last_successful_cycle=0.0,
                prom_failures=0,
                prom_last_failure_time=0.0,
            )
            existing.target_namespace = target_ns
            existing.target_deployment = target
            _cr_state[key] = existing
            logger.info(f"[{cr_name}] Initialized new CR state")

        # Resolve model bundle
        try:
            bundle = resolve_model_bundle(DEFAULT_MODEL_DIR, target_app, target_horizon)
        except BundlePendingError as exc:
            existing.model_load_pending = True
            logger.warning(f"[{cr_name}] Model bundle pending: {exc}")
            return config, existing
        except BundleValidationError as exc:
            existing.model_load_pending = False
            existing.last_failed_model_version = target_horizon
            existing.model_upgrade_failure_reason = str(exc)[:200]
            logger.error(f"[{cr_name}] Model bundle validation failed: {exc}")
            return config, existing

        # Load or update predictor
        model_path = str(bundle.model_path)
        scaler_path = str(bundle.scaler_path)
        target_scaler_path = str(bundle.target_scaler_path) if bundle.target_scaler_path else None
        metadata_path = str(bundle.metadata_path) if bundle.metadata_path else None

        # Check if we need to reload
        if existing.predictor and existing.active_model_version == bundle.version:
            existing.observer_mode = bool(config.get("observer_mode", False))
            return config, existing

        # Model upgrade or first load
        if existing.predictor:
            logger.info(f"[{cr_name}] Model upgraded, reloading interpreter...")
            existing.pending_model_version = bundle.version
            old_history = existing.predictor.copy_history()
        else:
            old_history = None

        try:
            from ppa.operator.predictor import Predictor

            new_predictor = Predictor(
                model_path,
                scaler_path,
                target_scaler_path,
                metadata_path=metadata_path,
                version=bundle.version,
            )
            if not new_predictor.is_loaded():
                raise RuntimeError(new_predictor.last_load_error or "predictor not loaded")

            # Restore history if upgrading
            if old_history:
                new_predictor.restore_history(old_history)
                logger.info(f"[{cr_name}] Restored {len(old_history)} history steps")

            existing.predictor = new_predictor
            existing.active_model_version = bundle.version
            existing.pending_model_version = None
            existing.last_failed_model_version = None
            existing.model_upgrade_failure_reason = None
            existing.model_load_pending = False
            existing.model_load_time_ms = new_predictor.model_load_time_ms

        except Exception as e:
            logger.error(f"[{cr_name}] Predictor load failed: {e}")
            if not existing.predictor:
                raise
            return config, existing

        return config, existing

# Reconciliation handler
@kopf.timer("ppa.example.com", "v1", "predictiveautoscalers", interval=TIMER_INTERVAL, initial_delay=INITIAL_DELAY)
def reconcile(spec, status, meta, patch, **kwargs):
    """Main control loop — delegates to ScalerStateMachine."""
    cr_ns = meta.get("namespace", NAMESPACE)
    cr_name = meta.get("name", "unknown")

    try:
        # 1. Parse CRD spec and load CR state
        config, state = _parse_crd_spec(spec, status, meta, cr_ns, cr_name, patch)
    except Exception as e:
        logger.error(f"[{cr_name}] CR reconciliation FAILED: {e}")
        return

    try:
        # 2. Create state machine and run reconciliation
        state_machine = ScalerStateMachine(
            cr_name=cr_name,
            cr_namespace=cr_ns,
            state=state,
            config=config,
            patch=patch,
            status=status,
        )

        # 3. Run full cycle
        status_update = state_machine.reconcile()

        # 4. Update status (throttled) - inline to avoid circular import
        if status_update:
            for key, value in status_update.items():
                if patch.status.get(key) != value:
                    patch.status[key] = value

    except Exception as e:
        logger.error(f"[{cr_name}] Reconciliation cycle error: {e}", exc_info=True)

# Cleanup on CR deletion
@kopf.on.delete("ppa.example.com", "v1", "predictiveautoscalers")
def on_delete(meta, **kwargs):
    """Clean up per-CR state when a CR is deleted."""
    key = (meta.get("namespace", NAMESPACE), meta.get("name", "unknown"))
    with _cr_state_lock:
        removed = _cr_state.pop(key, None)
        if removed:
            logger.info(f"Cleaned up state for {key}")

# Startup hook
@kopf.on.startup()
def startup(**kwargs):
    """Operator startup hook."""
    logger.info("=" * 80)
    logger.info("PPA Operator starting up")
    logger.info(f"DEFAULT_MODEL_DIR: {DEFAULT_MODEL_DIR}")
    logger.info("=" * 80)
