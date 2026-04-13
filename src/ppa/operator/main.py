# operator/main.py — kopf timer handler (multi-CR orchestrator)
"""PPA Operator: manages N PredictiveAutoscaler CRs independently."""

import logging
import math
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import kopf
from prometheus_client import Counter, Gauge
from prometheus_client import start_http_server as _prom_start_http_server

from ppa.config import (
    DEFAULT_CAPACITY_PER_POD,
    DEFAULT_MIN_REPLICAS,
    DEFAULT_MODEL_DIR,
    DEFAULT_SCALE_DOWN_RATE,
    DEFAULT_SCALE_UP_RATE,
    FEATURE_COLUMNS,
    INITIAL_DELAY,
    NAMESPACE,
    STABILIZATION_STEPS,
    STABILIZATION_TOLERANCE,
    TIMER_INTERVAL,
    FeatureVectorException,
)
from ppa.domain import CRState, calculate_replicas
from ppa.operator.features import (
    PrometheusCircuitBreakerTripped,
    build_feature_vector,
)
from ppa.operator.predictor import Predictor
from ppa.operator.scaler import scale_deployment

# ---------------------------------------------------------------------------
# Prometheus metrics — labelled by cr_name + namespace for multi-CR support
# ---------------------------------------------------------------------------
_LABELS = ["cr_name", "namespace"]

ppa_predicted_load_rps = Gauge("ppa_predicted_load_rps", "LSTM predicted load (req/s)", _LABELS)
ppa_inflated_load_rps = Gauge("ppa_inflated_load_rps", "predicted * safety_factor (req/s)", _LABELS)
ppa_raw_desired_replicas = Gauge("ppa_raw_desired_replicas", "Unclamped replica target", _LABELS)
ppa_desired_replicas = Gauge(
    "ppa_desired_replicas", "Rate-limited, bounds-clamped replicas", _LABELS
)
ppa_current_replicas = Gauge("ppa_current_replicas", "Observed ready replicas", _LABELS)
ppa_consecutive_skips = Gauge("ppa_consecutive_skips", "Cycles skipped due to bad data", _LABELS)
ppa_warmup_progress = Gauge("ppa_warmup_progress", "History window fill ratio (0-1)", _LABELS)
ppa_model_load_failed = Gauge("ppa_model_load_failed", "1 if model failed to load, else 0", _LABELS)
ppa_scale_events_total = Counter(
    "ppa_scale_events_total", "Total scaling decisions applied", _LABELS
)
ppa_circuit_breaker_tripped = Gauge(
    "ppa_circuit_breaker_tripped", "1 if circuit breaker active, 0 else", _LABELS
)
ppa_metric_failures = Gauge(
    "ppa_metric_failures", "Consecutive metric extraction failures", _LABELS
)
ppa_concept_drift_detected = Gauge(
    "ppa_concept_drift_detected", "1 if concept drift detected, 0 else", _LABELS
)
ppa_prediction_error_pct = Gauge(
    "ppa_prediction_error_pct", "Mean absolute percentage error %", _LABELS
)
ppa_inference_latency_ms = Gauge(
    "ppa_inference_latency_ms", "Model inference latency in ms", _LABELS
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("ppa.operator")

# Suppress kopf.objects verbose logging (patching, timers, etc.)
logging.getLogger("kopf.objects").setLevel(logging.ERROR)


# ---------------------------------------------------------------------------
# Health endpoint — lightweight HTTP server for liveness / readiness probes
# ---------------------------------------------------------------------------
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):  # noqa: A002 — suppress per-request logs
        pass


def _start_health_server(port: int = 8080):
    server = HTTPServer(("", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Health endpoint listening on :{port}/healthz")


def _update_status(patch: kopf.Patch, key: str, value: Any) -> None:
    """Idempotent status update: only write if value changed.

    Prevents patch conflicts by only updating status fields when the value
    actually differs from current state.
    """
    if patch.status.get(key) != value:
        patch.status[key] = value


def _ensure_model_directory_ready(max_retries: int = 5) -> None:
    """Startup guard: wait for model directory to exist before processing CRs.

    Used at operator startup to handle Kubernetes volume mount delays.
    FATAL after retries: raises RuntimeError if directory never appears.
    This ensures K8s knows operator failed to start and can restart the pod.
    """
    model_dir = DEFAULT_MODEL_DIR

    for attempt in range(max_retries):
        if os.path.exists(model_dir):
            logger.info(f"✓ Model directory ready: {model_dir}")
            return

        if attempt < max_retries - 1:
            wait_seconds = 2**attempt  # Exponential backoff: 1s, 2s, 4s, 8s, 16s
            logger.warning(
                f"Model directory not found (attempt {attempt + 1}/{max_retries}): {model_dir}\n"
                f"Retrying in {wait_seconds}s..."
            )
            time.sleep(wait_seconds)
        else:
            error_msg = (
                f"Model directory still not found after {max_retries} retries: {model_dir}\n"
                f"Check: 1) PPA_MODEL_DIR environment variable\n"
                f"       2) /models volume mount in Kubernetes\n"
                f"       3) Pod startup order (volume mount may be delayed)\n"
                f"This is a FATAL error - operator cannot start."
            )
            logger.critical(error_msg)
            raise RuntimeError(error_msg)


_start_health_server()

# Start Prometheus metrics endpoint on port 9100 (separate from healthz on 8080)
_prom_start_http_server(9100)
logging.getLogger("ppa.operator").info("Prometheus metrics endpoint listening on :9100/metrics")


# Registry keyed by (cr_namespace, cr_name) to avoid cross-namespace collisions.
# Thread-safe: protected by _cr_state_lock for concurrent reconciliation cycles
_cr_state: dict[tuple[str, str], CRState] = {}
_cr_state_lock = threading.Lock()


def _resolve_paths(
    spec: dict, target_app: str, target_horizon: str
) -> tuple[str | None, str | None, str | None, bool, bool]:
    """Resolve model/scaler/target paths. Supports two directory layouts.

    Layout 1 — structured (preferred, v2 architecture):
        /models/{app}/{horizon}/current/ppa_model.tflite
        /models/{app}/{horizon}/current/scaler.pkl
        /models/{app}/{horizon}/current/target_scaler.pkl   (optional)

    Layout 2 — flat (legacy upload-pod produces this):
        /models/{horizon}/ppa_model.tflite
        /models/{horizon}/scaler.pkl
        /models/{horizon}/target_scaler.pkl   (optional)

    The structured layout is tried first. If not present, the flat layout is
    tried as a fallback. This allows the operator to work with both layouts
    during a migration period.

    To migrate from flat → structured, create the directory and symlink:
        mkdir -p /models/{app}/{horizon}/v1
        cp /models/{horizon}/* /models/{app}/{horizon}/v1/
        ln -sfn /models/{app}/{horizon}/v1 /models/{app}/{horizon}/current

    CR spec fields (modelPath, scalerPath, targetScalerPath) are DEPRECATED
    and are ignored — the caller logs a one-time warning via the state flag.

    Returns:
        (model_path, scaler_path, target_scaler_path, used_legacy: bool, is_override: bool)
        Returns (None, None, None, False, False) if neither layout is ready.
    """
    model_dir = DEFAULT_MODEL_DIR

    # Layout 1: /models/{app}/{horizon}/current/  (structured, preferred)
    structured_base = os.path.join(model_dir, target_app, target_horizon, "current")
    if os.path.exists(structured_base):
        model_path = os.path.join(structured_base, "ppa_model.tflite")
        scaler_path = os.path.join(structured_base, "scaler.pkl")
        target_scaler_path = os.path.join(structured_base, "target_scaler.pkl")
        if not os.path.exists(target_scaler_path):
            target_scaler_path = None
        logger.debug(f"Resolved artifacts via structured layout: {structured_base}")
        return model_path, scaler_path, target_scaler_path, False, False

    # Layout 2: /models/{horizon}/  (flat, produced by current upload-pod)
    flat_base = os.path.join(model_dir, target_horizon)
    if os.path.exists(flat_base):
        model_path = os.path.join(flat_base, "ppa_model.tflite")
        scaler_path = os.path.join(flat_base, "scaler.pkl")
        target_scaler_path = os.path.join(flat_base, "target_scaler.pkl")
        if not os.path.exists(target_scaler_path):
            target_scaler_path = None
        logger.debug(
            f"Resolved artifacts via flat layout: {flat_base} "
            f"(migrate to {structured_base} for versioned upgrades)"
        )
        return model_path, scaler_path, target_scaler_path, False, False

    logger.debug(
        f"No model artifacts found for {target_app}/{target_horizon}. "
        f"Tried: {structured_base}  and  {flat_base}"
    )
    return None, None, None, False, False


def _validate_artifact_paths(
    model_path: str,
    scaler_path: str,
    target_scaler_path: str | None,
    target_app: str,
    target_horizon: str,
    model_dir: str,
) -> None:
    """Validate artifact paths exist. Raises RuntimeError if ANY missing.

    Called ONCE on first load or path change, not every reconciliation cycle.
    """
    missing = []

    if not os.path.exists(model_path):
        missing.append(f"Model: {model_path}")
    if not os.path.exists(scaler_path):
        missing.append(f"Scaler: {scaler_path}")
    if target_scaler_path and not os.path.exists(target_scaler_path):
        missing.append(f"Target scaler: {target_scaler_path}")

    if missing:
        raise RuntimeError(
            f"Missing model artifacts for {target_app}/{target_horizon}:\n"
            + "\n".join(f"  ❌ {path}" for path in missing)
            + "\nExpected directory structure:\n"
            + f"  {model_dir}/{target_app}/{target_horizon}/\n"
            + "    ├─ ppa_model.tflite\n"
            + "    ├─ scaler.pkl\n"
            + "    └─ target_scaler.pkl (optional)"
        )


def _get_or_create_state(
    key: tuple[str, str],
    model_path: str,
    scaler_path: str,
    target_scaler_path: str | None = None,
    config: dict[str, Any] | None = None,
    target_app: str = "",
    target_ns: str = "",
    max_r: int = 1,
    container_name: str | None = None,
    min_r: int = 1,
    persisted_history: dict | None = None,
    target_horizon: str = "",
) -> tuple[CRState, dict[str, Any]]:
    """Lazy-init or reload CRState if model paths changed.

    FIX (PR#3): Prefill is SKIPPED to avoid scaler distribution mismatch.
    FIX (PR#5): History is PRESERVED when model is upgraded (new paths), preventing 30-min blindness.
    FIX (PR#15): Restore history from CR status on pod restart.
    FIX (Phase 3): Full function lock (Issue 2) + try-catch on Predictor creation (Issue 17)

    Thread-safe: protected by _cr_state_lock for entire function (sole lock owner).

    Returns:
        Tuple of (CRState, upgrade_info dict with keys: upgraded, failed_to_upgrade, reason)
    """
    # SOLE LOCK OWNER: wrap entire function body (Issue 2)
    with _cr_state_lock:
        existing = _cr_state.get(key)
        if (
            existing
            and existing.predictor
            and existing.predictor.paths_match(model_path, scaler_path, target_scaler_path)
        ):
            existing.observer_mode = bool((config or {}).get("observer_mode", False))
            return existing, {"upgraded": False, "failed_to_upgrade": False, "reason": None}

        if existing and existing.predictor:
            # Model upgraded: preserve history, reload interpreter only (PR#5 fix)
            logger.info(f"Model upgraded for {key}, reloading interpreter (preserving history)...")

            # Validate new paths BEFORE creating new predictor
            try:
                _validate_artifact_paths(
                    model_path,
                    scaler_path,
                    target_scaler_path,
                    target_app,
                    target_horizon,
                )
            except RuntimeError as e:
                # PRODUCTION-SAFE: Don't break working system on upgrade failure
                # Keep old predictor running, log error clearly, signal upgrade failure
                error_msg = str(e)
                logger.error(
                    f"[{key}] Model upgrade FAILED - keeping old model active: {error_msg}"
                )
                existing.observer_mode = bool((config or {}).get("observer_mode", False))
                return existing, {
                    "upgraded": False,
                    "failed_to_upgrade": True,
                    "reason": error_msg[:200],
                }

            # Log successful new paths (INFO level - this is a change)
            logger.info(
                f"[{key}] Resolved upgraded paths:\n"
                f"  Model:  {model_path}\n"
                f"  Scaler: {scaler_path}\n"
                f"  Target: {target_scaler_path or '(optional, not found)'}"
            )

            # Snapshot history before creating new predictor
            old_history = existing.predictor.copy_history()
            history_len = len(old_history)

            # Issue 17: Try-catch around Predictor creation (file exists but corrupted/unreadable)
            try:
                new_predictor = Predictor(model_path, scaler_path, target_scaler_path)
            except Exception as e:
                logger.error(
                    f"[{key}] Predictor load FAILED despite file existence check: {e}\n"
                    f"  Model: {model_path}\n"
                    f"  Scaler: {scaler_path}\n"
                    f"  Possible causes: file corruption, wrong format, permissions, NFS stale metadata"
                )
                # Keep old predictor, signal upgrade failure
                return existing, {
                    "upgraded": False,
                    "failed_to_upgrade": True,
                    "reason": f"Predictor load failed: {str(e)[:100]}",
                }

            # Restore history into new predictor
            new_predictor.restore_history(old_history)

            # Update state in-place (don't create new CRState)
            existing.predictor = new_predictor
            existing.observer_mode = bool((config or {}).get("observer_mode", False))

            logger.info(f"Restored {history_len}/{60} history steps to new model")
            return existing, {"upgraded": True, "failed_to_upgrade": False, "reason": None}

        # First time: VALIDATE BEFORE creating new state (fail hard on missing artifacts)
        try:
            _validate_artifact_paths(
                model_path,
                scaler_path,
                target_scaler_path,
                target_app,
                target_horizon,
                DEFAULT_MODEL_DIR,
            )
        except RuntimeError as e:
            logger.error(f"[{key}] {e}")
            raise  # Fail immediately - operator will retry at appropriate interval

        # Log successful paths (INFO level - this is a new state)
        logger.info(
            f"[{key}] Resolved paths:\n"
            f"  Model:  {model_path}\n"
            f"  Scaler: {scaler_path}\n"
            f"  Target: {target_scaler_path or '(optional, not found)'}"
        )

        # Issue 17: Try-catch around first-time Predictor creation
        try:
            predictor = Predictor(model_path, scaler_path, target_scaler_path)
        except Exception as e:
            logger.error(
                f"[{key}] First-time predictor load FAILED despite validation: {e}\n"
                f"  Model: {model_path}\n"
                f"  Scaler: {scaler_path}"
            )
            raise

        state = CRState(
            predictor=predictor,
            observer_mode=bool((config or {}).get("observer_mode", False)),
        )

        # FIX (PR#15): Restore history from CR status if available (pod restart resilience)
        if persisted_history and persisted_history.get("data"):
            try:
                history_data = persisted_history["data"]
                restored = state.predictor.deserialize_history(history_data)
                if restored:
                    logger.info(
                        f"[{key}] Restored {len(history_data)} history steps from CR status (PR#15)"
                    )
                else:
                    logger.warning(
                        f"[{key}] Failed to restore history from CR status, starting fresh"
                    )
            except Exception as exc:
                logger.warning(f"[{key}] Error restoring history: {exc}, starting fresh")
        else:
            logger.info(
                f"[{key}] Skipping prefill to avoid scaler distribution mismatch (cold-start: ~30 min warmup)"
            )

        _cr_state[key] = state
        return state, {"upgraded": False, "failed_to_upgrade": False, "reason": None}


def _parse_crd_spec(
    spec: dict[str, Any],
    status: dict[str, Any] | None,
    meta: dict[str, Any],
    cr_ns: str,
    cr_name: str,
    patch: kopf.Patch,
) -> tuple[dict[str, Any], CRState]:
    """Parse CRD spec into config and load/create CR state. Extract state management logic.

    Returns:
        Tuple of (config dict, CRState)
    """
    target = spec["targetDeployment"]
    target_ns = spec.get("namespace", cr_ns)
    target_app = spec.get("appName", target)
    target_horizon = spec.get("horizon", "rps_t3m")

    # Validate required fields
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

    # Detect multiple CRs managing the SAME deployment (thread-safe check)
    # Only warn when another non-observer CR targets the same namespace/deployment pair.
    # Checking all peer CRs regardless of their target caused false positives when
    # multiple CRs manage *different* deployments (a perfectly valid use-case).
    key = (cr_ns, cr_name)

    # Initialize state early enough for conflict_logged access below
    _existing_pre = _cr_state.get(key)

    with _cr_state_lock:
        peer_target_crs = [
            k
            for k, s in _cr_state.items()
            if k != key
            and not s.observer_mode
            and getattr(s, "target_deployment", None) == f"{target_ns}/{target}"
        ]
    if peer_target_crs and not config["observer_mode"]:
        # Log once on first detection; suppress repeats to avoid flooding logs every cycle.
        conflict_logged = getattr(_existing_pre, "conflict_logged", False)
        if not conflict_logged:
            logger.warning(
                f"[{cr_name}] Multiple CRs detected managing the same target "
                f"{target_ns}/{target}. Ensure only ONE active CR manages this deployment "
                f"to avoid scaling oscillations. "
                f"Set observerMode: true on all but one CR. "
                f"Conflicting CRs: {peer_target_crs}"
            )
            if _existing_pre is not None:
                _existing_pre.conflict_logged = True

    # STEP 1: Resolve paths (spec override > canonical > legacy > missing)
    model_path, scaler_path, target_scaler_path, used_legacy, is_override = _resolve_paths(
        spec, target_app, target_horizon
    )

    # STEP 2: Get or initialize state (double-check locking - Issue 12)
    existing = _cr_state.get(key)

    if existing is None:
        with _cr_state_lock:
            # Check AGAIN inside lock (another thread may have created it)
            existing = _cr_state.get(key)
            if existing is None:
                # Safe to create: we're the only thread doing this now
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
                existing.artifact_load_failures = 0
                existing.using_legacy_artifacts = False
                existing.predictor_missing_logged = False
                existing.deprecation_logged = False
                existing.target_scaler_missing_logged = False
                existing.target_deployment = f"{target_ns}/{target}"
                existing.conflict_logged = False  # Only warn on first detection

                _cr_state[key] = existing
                logger.info(f"[{cr_name}] Initialized new CR state")

    # DEPRECATION WARNING (log once only)
    if spec.get("modelPath") or spec.get("scalerPath"):
        if not existing.deprecation_logged:
            logger.warning(
                f"[{cr_name}] modelPath/scalerPath/targetScalerPath are deprecated and ignored. "
                f"Artifacts are now loaded from /models/{{app}}/{{horizon}}/current/. "
                f"Remove these fields from CR spec."
            )
            existing.deprecation_logged = True

    # STEP 2b: Handle None paths from _resolve_paths (graceful retry)
    if model_path is None:
        with _cr_state_lock:
            existing.artifact_load_failures += 1
            failures = existing.artifact_load_failures

        if failures < 3:
            logger.debug(f"[{cr_name}] Artifacts not ready (base missing). Will retry...")
        elif failures == 3:
            logger.error(
                f"[{cr_name}] Artifacts unavailable after {failures} attempts. "
                f"Check /models/{target_app}/{config['target_horizon']}/current/ symlink. "
                f"Run 'ppa model train --app {target_app} --horizon {config['target_horizon']}' "
                f"to generate artifacts."
            )
        elif failures % 10 == 0:
            logger.error(
                f"[{cr_name}] Still no artifacts after {failures} attempts. "
                f"/models/{target_app}/{config['target_horizon']}/current/ symlink missing."
            )
        return config, existing

    # STEP 3: Apply legacy flag transitions (locked + stable - Issues 14, 16)
    if used_legacy and not existing.using_legacy_artifacts:
        with _cr_state_lock:
            existing.using_legacy_artifacts = True
        if not patch.status.get("usingLegacyArtifacts"):
            patch.status["usingLegacyArtifacts"] = True
            logger.warning(
                f"[{cr_name}] Using legacy artifact paths. "
                f"Consider updating CR spec to use canonical paths."
            )

    elif (
        existing.using_legacy_artifacts
        and not used_legacy
        and os.path.exists(model_path)
        and os.path.exists(scaler_path)
    ):
        with _cr_state_lock:
            existing.using_legacy_artifacts = False
        if patch.status.get("usingLegacyArtifacts"):
            patch.status["usingLegacyArtifacts"] = False
            logger.info(f"[{cr_name}] Switched to canonical artifact paths")

    # STEP 4: Check existence & retry logic (locked, all-or-nothing - Issues 4, 14, 17, 18, 19)
    missing_paths = []
    if model_path is None or not os.path.exists(model_path):
        missing_paths.append(f"Model: {model_path or 'unknown'}")
    if scaler_path is None or not os.path.exists(scaler_path):
        missing_paths.append(f"Scaler: {scaler_path or 'unknown'}")

    # Handle target_scaler (optional, log once)
    if target_scaler_path and not os.path.exists(target_scaler_path):
        if not existing.target_scaler_missing_logged:
            logger.warning(
                f"[{cr_name}] Target scaler missing at {target_scaler_path}, continuing without it"
            )
            existing.target_scaler_missing_logged = True
        target_scaler_path = None

    if missing_paths:
        # Increment counter UNDER LOCK (Issue 14)
        with _cr_state_lock:
            existing.artifact_load_failures += 1
            failures = existing.artifact_load_failures

        if failures < 3:
            # Retry window: log but don't fail
            missing_str = ", ".join(missing_paths)
            if is_override:
                logger.debug(
                    f"[{cr_name}] CR-specified paths not ready (attempt {failures}/3). "
                    f"Check configuration:\n  {missing_str}\n"
                    f"Will retry..."
                )
            else:
                logger.debug(
                    f"[{cr_name}] Artifact not ready (attempt {failures}/3). "
                    f"Missing: {missing_str}. Will retry..."
                )
            return config, existing
        else:
            # After 3 failures: signal failure with error classification (Issues 18, 19)
            missing_str = ", ".join(missing_paths)
            error_msg = f"Missing after {failures} retries: {missing_str}"
            error_type = "USER_CONFIG" if is_override else "SYSTEM_DELAY"

            # Update status (idempotent - Issue 15)
            if not patch.status.get("artifactLoadFailed"):
                patch.status["artifactLoadFailed"] = True
                patch.status["artifactLoadError"] = error_msg
                patch.status["artifactLoadErrorType"] = error_type

            # Periodic escalation logging (Issue 18)
            if failures == 3:
                # First-time failure log
                if is_override:
                    logger.error(
                        f"[{cr_name}] ARTIFACT LOAD FAILED (CR override misconfigured): "
                        f"{failures} retries exhausted.\n"
                        f"  {missing_str}\n"
                        f"Action: Verify CR spec paths are correct."
                    )
                else:
                    logger.error(
                        f"[{cr_name}] ARTIFACT LOAD FAILED: {failures} retries exhausted. "
                        f"Missing: {missing_str}. Continuing with null predictor (scaling disabled)."
                    )
            elif failures % 10 == 0:
                # Periodic re-escalation every 10 failures (Issue 18)
                logger.error(
                    f"[{cr_name}] STILL FAILING after {failures} attempts (status=artifactLoadFailed). "
                    f"Artifacts remain unavailable. "
                    f"Type: {error_type}. "
                    f"Action: {'Fix CR spec' if is_override else 'Investigate cluster state, PVC, artifact job'}"
                )
            else:
                # Ensure errorType is always set (idempotent - Issue 15)
                if patch.status.get("artifactLoadErrorType") != error_type:
                    patch.status["artifactLoadErrorType"] = error_type

            return config, existing

    # Paths exist: reset counter and clear failure state (locked - Issue 14, idempotent - Issue 15)
    with _cr_state_lock:
        if existing.artifact_load_failures > 0:
            existing.artifact_load_failures = 0
            should_clear_failure = True
        else:
            should_clear_failure = False

    if should_clear_failure:
        logger.info(f"[{cr_name}] Artifacts recovered (counter reset)")

        # Clear all failure-related status fields (Issue 10, 15)
        if patch.status.get("artifactLoadFailed"):
            patch.status["artifactLoadFailed"] = False
            patch.status["artifactLoadError"] = None
            patch.status["artifactLoadErrorType"] = None
            logger.info(f"[{cr_name}] Cleared artifact failure signal")

    # STEP 5: Call _get_or_create_state (paths validated, paths exist)
    # STEP 5: Call _get_or_create_state (paths validated, paths exist)
    state, upgrade_info = _get_or_create_state(
        key,
        model_path,
        scaler_path,
        target_scaler_path,
        config,
        target,
        target_ns,
        max_r,
        config["container_name"],
        config["min_r"],
        None,  # persisted_history removed (no longer stored in status)
        config["target_horizon"],
    )

    # STEP 6: Re-fetch state (fresh reference after lock release in _get_or_create_state)
    state = _cr_state.get(key)

    # Handle upgrade_info (idempotent status updates - Issue 15)
    if upgrade_info["failed_to_upgrade"]:
        if not patch.status.get("modelUpgradeFailed"):
            patch.status["modelUpgradeFailed"] = True
            patch.status["modelUpgradeFailureReason"] = upgrade_info["reason"]
            logger.warning(
                f"[{cr_name}] Model upgrade failed - running on old model. "
                f"Reason: {upgrade_info['reason']}"
            )
    elif upgrade_info["upgraded"]:
        if patch.status.get("modelUpgradeFailed"):
            patch.status["modelUpgradeFailed"] = False
            patch.status["modelUpgradeFailureReason"] = None
            logger.info(f"[{cr_name}] Model upgraded successfully")
    else:
        # Only set if not already in status (idempotent - Issue 15)
        if "modelUpgradeFailed" not in (status or {}):
            patch.status["modelUpgradeFailed"] = False

    return config, state


def _fetch_and_validate_features(
    config: dict[str, Any],
    cr_name: str,
    cr_ns: str,
    state: CRState,
    patch: kopf.Patch,
    status: dict[str, Any] | None,
) -> tuple[dict[str, Any], int, bool]:
    """Fetch features from Prometheus and handle failures. Extract feature acquisition and error handling.

    Returns:
        Tuple of (features dict, current_replicas, should_continue)
    """
    try:
        features, current_replicas = build_feature_vector(
            config["target"],
            config["target_ns"],
            config["min_r"],
            config["max_r"],
            config["container_name"],
            config["prom_url"],
            state,
        )
        state.last_successful_cycle = time.time()
        state.consecutive_failures = 0
        state.last_known_good_replicas = int(current_replicas)
        return features, int(current_replicas), True

    except (FeatureVectorException, PrometheusCircuitBreakerTripped) as e:
        state.consecutive_failures += 1
        metric_failures = (status.get("metricFailures", 0) if status else 0) + 1

        logger.error(f"[{cr_name}] Feature extraction failed ({metric_failures}/5): {e}")

        if state.last_known_good_replicas > 0 and state.consecutive_failures <= 3:
            fallback_replicas = state.last_known_good_replicas
            logger.warning(
                f"[{cr_name}] Using fallback scaling: {fallback_replicas} replicas "
                f"(last known good, failure {state.consecutive_failures}/3)"
            )

            if not config["observer_mode"]:
                logger.info(
                    f"[{cr_name}] FALLBACK: Scaling {config['target_ns']}/{config['target']} "
                    f"to {fallback_replicas} replicas"
                )
                scale_deployment(config["target"], fallback_replicas, config["target_ns"])

            return {}, 0, False

        if metric_failures >= 5:
            ppa_circuit_breaker_tripped.labels(cr_name=cr_name, namespace=cr_ns).set(1)
            logger.critical(
                f"[{cr_name}] CIRCUIT BREAKER TRIPPED after {metric_failures} metric failures. "
                f"PPA will not scale until metrics recover."
            )
        else:
            ppa_metric_failures.labels(cr_name=cr_name, namespace=cr_ns).set(metric_failures)

        return {}, 0, False


def _update_predictor_state(
    cr_name: str,
    cr_ns: str,
    config: dict[str, Any],
    state: CRState,
    features: dict[str, Any],
    current: int,
    patch: kopf.Patch,
) -> bool:
    """Update predictor history and check readiness. Extract model warmup logic.

    Returns:
        True if predictor is ready for prediction, False otherwise
    """
    # Guard: If predictor not ready (e.g., during artifact retry), return early
    if state.predictor is None:
        if not state.predictor_missing_logged:
            logger.debug(
                f"[{cr_name}] Predictor not available yet. Waiting for artifacts to load..."
            )
            state.predictor_missing_logged = True
        patch.status["currentReplicas"] = current
        return False

    state.predictor.update(features)
    history_len = len(state.predictor.history)
    maxlen = state.predictor.history.maxlen

    ppa_warmup_progress.labels(cr_name=cr_name, namespace=cr_ns).set(
        history_len / maxlen if maxlen else 0
    )
    ppa_model_load_failed.labels(cr_name=cr_name, namespace=cr_ns).set(
        1 if state.predictor._load_failed else 0
    )
    patch.status["currentReplicas"] = current

    if not state.predictor.ready():
        if state.predictor._load_failed:
            logger.debug(
                f"[{cr_name}] Model not loaded (will retry next cycle). "
                f"History: {history_len}/{maxlen}"
            )
        else:
            logger.info(f"[{cr_name}] Warming up: {history_len}/{maxlen} steps collected")
        return False

    # Reset transition flag when predictor becomes ready
    state.predictor_missing_logged = False

    logger.info(
        f"[{cr_name}] RPS/Pod={features['rps_per_replica']:.1f}  "
        f"P95={features['latency_p95_ms']:.1f}ms  "
        f"CPU={features['cpu_utilization_pct']:.1f}%  "
        f"Replicas={features['replicas_normalized']:.2f} (norm)"
    )
    return True


def _make_scaling_decision(
    cr_name: str,
    cr_ns: str,
    config: dict[str, Any],
    state: CRState,
    features: dict[str, Any],
    current: int,
    patch: kopf.Patch,
) -> tuple[int, bool]:
    """Predict load, check drift, stabilize, and calculate target replicas. Extract scaling logic.

    Returns:
        Tuple of (desired_replicas, should_apply_scaling)
    """
    # Guard: If predictor not ready, skip scaling (quietly with debug level)
    if state.predictor is None:
        logger.debug(f"[{cr_name}] Skipping scaling (predictor not ready yet)")
        return current, False

    predicted_load = state.predictor.predict()
    logger.info(f"[{cr_name}] Predicted load: {predicted_load:.1f} req/s")

    ppa_predicted_load_rps.labels(cr_name=cr_name, namespace=cr_ns).set(predicted_load)

    # Track prediction accuracy and check for concept drift
    actual_rps = features.get("requests_per_second", 0.0)
    state.predictor.track_prediction_accuracy(predicted_load, actual_rps)

    drift_check = state.predictor.check_concept_drift()
    if drift_check.get("checked") and drift_check.get("detected"):
        error_pct = drift_check.get("error_pct", 0)
        severity = drift_check.get("severity", "unknown")
        ppa_concept_drift_detected.labels(cr_name=cr_name, namespace=cr_ns).set(1)
        ppa_prediction_error_pct.labels(cr_name=cr_name, namespace=cr_ns).set(error_pct)
        logger.error(
            f"[{cr_name}] CONCEPT DRIFT ({severity}): "
            f"Prediction error {error_pct:.1f}% (threshold: 20%). "
            f"Consider retraining the model."
        )

        retraining_check = state.predictor.should_trigger_retraining(severity, error_pct)
        if retraining_check.get("trigger"):
            logger.critical(
                f"[{cr_name}] RETRAINING TRIGGERED: {retraining_check.get('reason')}. "
                f"Please run 'ppa model retrain --app {config['target_app']} "
                f"--horizon {config['target_horizon']}' or enable auto-retraining in CR spec."
            )
    else:
        ppa_concept_drift_detected.labels(cr_name=cr_name, namespace=cr_ns).set(0)

    # Calculate candidate replicas with safety factor
    state.last_prediction = predicted_load
    capacity = config["capacity"] if config["capacity"] is not None else DEFAULT_CAPACITY_PER_POD
    inflated = predicted_load * config["safety_factor"]
    raw_desired = math.ceil(inflated / capacity) if capacity else current
    ppa_inflated_load_rps.labels(cr_name=cr_name, namespace=cr_ns).set(inflated)
    ppa_raw_desired_replicas.labels(cr_name=cr_name, namespace=cr_ns).set(raw_desired)

    candidate = calculate_replicas(
        predicted_load,
        current,
        config["min_r"],
        config["max_r"],
        config["capacity"],
        config["up_rate"],
        config["down_rate"],
        config["safety_factor"],
    )

    # Tolerance-based stabilization
    if abs(candidate - state.last_desired) <= STABILIZATION_TOLERANCE:
        state.stable_count += 1
    else:
        state.stable_count = 1

    state.last_desired = float(candidate)

    if state.stable_count < STABILIZATION_STEPS:
        logger.info(
            f"[{cr_name}] Stabilizing: {state.stable_count}/{STABILIZATION_STEPS} "
            f"(target: {candidate} replicas, tolerance: ±{STABILIZATION_TOLERANCE})"
        )
        ppa_desired_replicas.labels(cr_name=cr_name, namespace=cr_ns).set(candidate)
        return candidate, False

    ppa_desired_replicas.labels(cr_name=cr_name, namespace=cr_ns).set(candidate)
    return candidate, True


def _apply_scaling(
    cr_name: str,
    cr_ns: str,
    config: dict[str, Any],
    state: CRState,
    desired: int,
    current: int,
    patch: kopf.Patch,
) -> None:
    """Apply scaling decision to deployment. Extract scaling action logic.

    Args:
        desired: Target replica count
        current: Current replica count
    """

    if desired != current:
        if config["observer_mode"]:
            logger.info(
                f"[{cr_name}] OBSERVER: would scale {config['target_ns']}/{config['target']}: "
                f"{current} → {desired} (skipped — observerMode=true)"
            )
        else:
            logger.info(
                f"[{cr_name}] Scaling {config['target_ns']}/{config['target']}: {current} → {desired}"
            )
            scale_deployment(config["target"], desired, config["target_ns"])
            ppa_scale_events_total.labels(cr_name=cr_name, namespace=cr_ns).inc()
            state.stable_count = 0
    else:
        logger.info(f"[{cr_name}] No scaling needed: {current} replicas is correct")


@kopf.on.startup()
def startup(logger_: kopf.Logger = None, **kwargs):
    """Operator startup hook - runs once when operator pod starts."""
    pass
    logger.info("=" * 80)
    logger.info("PPA Operator starting up")
    logger.info(f"DEFAULT_MODEL_DIR: {DEFAULT_MODEL_DIR}")
    logger.info("=" * 80)

    # Check model directory is accessible
    _ensure_model_directory_ready()


@kopf.timer(
    "ppa.example.com",
    "v1",
    "predictiveautoscalers",
    interval=TIMER_INTERVAL,
    initial_delay=INITIAL_DELAY,
)
def reconcile(spec, status, meta, patch, **kwargs):
    """Main control loop — runs every TIMER_INTERVAL seconds per CR.

    Orchestrates: CRD parsing → feature fetching → prediction → scaling decision → apply.
    CR-level error isolation: any exception in one CR doesn't affect others.
    """
    cr_ns = meta.get("namespace", NAMESPACE)
    cr_name = meta.get("name", "unknown")

    try:
        # 1. Parse CRD spec and load CR state
        config, state = _parse_crd_spec(spec, status, meta, cr_ns, cr_name, patch)
    except Exception as e:
        # CR-level isolation: log error, mark CR as failed, don't crash operator
        logger.error(f"[{cr_name}] CR reconciliation FAILED: {e}")
        return

    try:
        # 2. Fetch features from Prometheus with error handling
        features, current, should_continue = _fetch_and_validate_features(
            config, cr_name, cr_ns, state, patch, status
        )
        if not should_continue:
            return

        # Reset metric failure counters on success
        patch.status["circuitBreakerTripped"] = False
        ppa_circuit_breaker_tripped.labels(cr_name=cr_name, namespace=cr_ns).set(0)
        ppa_metric_failures.labels(cr_name=cr_name, namespace=cr_ns).set(0)

        assert list(features.keys()) == FEATURE_COLUMNS, (
            f"[{cr_name}] Feature vector order mismatch"
        )

        # 3. Update predictor and check readiness
        ready = _update_predictor_state(cr_name, cr_ns, config, state, features, current, patch)
        if not ready:
            return

        # 4. Make scaling decision
        desired, should_apply = _make_scaling_decision(
            cr_name, cr_ns, config, state, features, current, patch
        )
        if not should_apply:
            return

        # 5. Apply scaling
        _apply_scaling(cr_name, cr_ns, config, state, desired, current, patch)

        # Clear any previous error state on success

    except Exception as e:
        # Reconciliation logic failed - log clearly and mark CR
        logger.error(f"[{cr_name}] Reconciliation cycle error: {e}", exc_info=True)
        # Don't re-raise - let operator continue with other CRs


@kopf.on.delete("ppa.example.com", "v1", "predictiveautoscalers")
def on_delete(meta, **kwargs):
    """Clean up per-CR state when a CR is deleted (thread-safe)."""
    key = (meta.get("namespace", NAMESPACE), meta.get("name", "unknown"))
    with _cr_state_lock:
        removed = _cr_state.pop(key, None)
    if removed:
        logger.info(f"Cleaned up state for {key}")
