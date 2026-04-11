# operator/main.py — kopf timer handler (multi-CR orchestrator)
"""PPA Operator: manages N PredictiveAutoscaler CRs independently."""

import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
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


def _resolve_paths(spec: dict, target_app: str, target_horizon: str) -> tuple[str, str, str | None]:
    """Compute model + scaler + target_scaler paths from CRD spec, falling back to convention.

    ⚠️ RESOLVES ONLY - validation happens in _get_or_create_state().
    This keeps reconciliation loop clean (no validation failures per cycle).
    """
    model_dir = DEFAULT_MODEL_DIR
    model_path = spec.get("modelPath") or os.path.join(
        model_dir, target_app, target_horizon, "ppa_model.tflite"
    )
    scaler_path = spec.get("scalerPath") or os.path.join(
        model_dir, target_app, target_horizon, "scaler.pkl"
    )
    # Target scaler is optional (backward compat with models trained without it)
    target_scaler_path = spec.get("targetScalerPath") or os.path.join(
        model_dir, target_app, target_horizon, "target_scaler.pkl"
    )
    if not os.path.exists(target_scaler_path):
        target_scaler_path = None
    return model_path, scaler_path, target_scaler_path


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
            + f"\nExpected directory structure:\n"
            + f"  {model_dir}/{target_app}/{target_horizon}/\n"
            + f"    ├─ ppa_model.tflite\n"
            + f"    ├─ scaler.pkl\n"
            + f"    └─ target_scaler.pkl (optional)"
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
) -> tuple[CRState, dict[str, Any]]:
    """Lazy-init or reload CRState if model paths changed.

    FIX (PR#3): Prefill is SKIPPED to avoid scaler distribution mismatch.
    FIX (PR#5): History is PRESERVED when model is upgraded (new paths), preventing 30-min blindness.
    FIX (PR#15): Restore history from CR status on pod restart.

    Thread-safe: protected by _cr_state_lock for concurrent reconciliation cycles.

    Returns:
        Tuple of (CRState, upgrade_info dict with keys: upgraded, failed_to_upgrade, reason)
    """
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
                    DEFAULT_MODEL_DIR,
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

            # Create new predictor with upgraded model
            new_predictor = Predictor(model_path, scaler_path, target_scaler_path)

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

        state = CRState(
            predictor=Predictor(model_path, scaler_path, target_scaler_path),
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

    # Detect multiple CRs managing the same deployment (thread-safe check)
    key = (cr_ns, cr_name)
    with _cr_state_lock:
        active_peer_crs = [
            k for k, state in _cr_state.items() if k != key and not state.observer_mode
        ]
    if (
        len(active_peer_crs) > 0
        and cr_name != "single-ppa-controller"
        and not config["observer_mode"]
    ):
        logger.warning(
            f"[{cr_name}] Multiple CRs detected in system. Ensure only ONE CR manages "
            f"target {target_ns}/{target} to avoid scaling oscillations."
        )

    # Load/create CR state
    model_path, scaler_path, target_scaler_path = _resolve_paths(spec, target_app, target_horizon)
    persisted_history = status.get("historySnapshot") if status else None
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
        persisted_history,
    )

    # Update CR status with upgrade signals
    if upgrade_info["failed_to_upgrade"]:
        patch.status["modelUpgradeFailed"] = True
        patch.status["modelUpgradeFailureReason"] = upgrade_info["reason"]
        logger.warning(
            f"[{cr_name}] Model upgrade failed - running on old model. Reason: {upgrade_info['reason']}"
        )
    elif upgrade_info["upgraded"]:
        patch.status["modelUpgradeFailed"] = False
        patch.status["modelUpgradeFailureReason"] = None
        logger.info(f"[{cr_name}] Model upgraded successfully")
    else:
        # Only clear upgrade failure if this is first initialization
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
        patch.status["metricFailures"] = metric_failures
        patch.status["lastMetricError"] = str(e)
        patch.status["lastMetricErrorTime"] = datetime.now(timezone.utc).isoformat()

        logger.error(f"[{cr_name}] Feature extraction failed ({metric_failures}/5): {e}")

        if state.last_known_good_replicas > 0 and state.consecutive_failures <= 3:
            fallback_replicas = state.last_known_good_replicas
            logger.warning(
                f"[{cr_name}] Using fallback scaling: {fallback_replicas} replicas "
                f"(last known good, failure {state.consecutive_failures}/3)"
            )
            patch.status["fallbackScaling"] = True
            patch.status["fallbackReason"] = f"Feature extraction failed: {str(e)[:100]}"
            patch.status["fallbackReplicas"] = fallback_replicas

            if not config["observer_mode"]:
                logger.info(
                    f"[{cr_name}] FALLBACK: Scaling {config['target_ns']}/{config['target']} "
                    f"to {fallback_replicas} replicas"
                )
                scale_deployment(config["target"], fallback_replicas, config["target_ns"])

            return {}, 0, False

        if metric_failures >= 5:
            patch.status["circuitBreakerTripped"] = True
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

    # Persist history for pod restart resilience
    if maxlen and history_len >= maxlen * 0.9:
        serialized = state.predictor.serialize_history()
        if serialized:
            patch.status["historySnapshot"] = {
                "steps": len(serialized),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "data": serialized[-30:] if len(serialized) > 30 else serialized,
            }

    if not state.predictor.ready():
        if state.predictor._load_failed:
            logger.warning(
                f"[{cr_name}] Model not loaded (will retry next cycle). "
                f"History: {history_len}/{maxlen}"
            )
        else:
            logger.info(f"[{cr_name}] Warming up: {history_len}/{maxlen} steps collected")
        return False

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
    predicted_load = state.predictor.predict()
    logger.info(f"[{cr_name}] Predicted load: {predicted_load:.1f} req/s")

    patch.status["lastPredictedLoad"] = round(predicted_load, 2)
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
        patch.status["conceptDriftDetected"] = True
        patch.status["driftSeverity"] = severity
        patch.status["predictionErrorPct"] = round(error_pct, 2)
        logger.error(
            f"[{cr_name}] CONCEPT DRIFT ({severity}): "
            f"Prediction error {error_pct:.1f}% (threshold: 20%). "
            f"Consider retraining the model."
        )

        retraining_check = state.predictor.should_trigger_retraining(severity, error_pct)
        if retraining_check.get("trigger"):
            patch.status["retrainingRecommended"] = True
            patch.status["retrainingReason"] = retraining_check.get("reason")
            logger.critical(
                f"[{cr_name}] RETRAINING TRIGGERED: {retraining_check.get('reason')}. "
                f"Please run 'ppa model retrain --app {config['target_app']} "
                f"--horizon {config['target_horizon']}' or enable auto-retraining in CR spec."
            )
    else:
        ppa_concept_drift_detected.labels(cr_name=cr_name, namespace=cr_ns).set(0)
        patch.status["conceptDriftDetected"] = False
        patch.status["retrainingRecommended"] = False

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
        patch.status["desiredReplicas"] = candidate
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
    patch.status["desiredReplicas"] = desired

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
            patch.status["lastScaleTime"] = datetime.now(timezone.utc).isoformat()
            ppa_scale_events_total.labels(cr_name=cr_name, namespace=cr_ns).inc()
            state.stable_count = 0
    else:
        logger.info(f"[{cr_name}] No scaling needed: {current} replicas is correct")


@kopf.on.startup()
def startup(logger_: kopf.Logger, **kwargs):
    """Operator startup hook - runs once when operator pod starts."""
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
        patch.status["lastError"] = str(e)[:500]
        patch.status["lastErrorTime"] = datetime.now(timezone.utc).isoformat()
        patch.status["reconciliationFailed"] = True
        return

    try:
        # 2. Fetch features from Prometheus with error handling
        features, current, should_continue = _fetch_and_validate_features(
            config, cr_name, cr_ns, state, patch, status
        )
        if not should_continue:
            return

        # Reset metric failure counters on success
        patch.status["metricFailures"] = 0
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
        patch.status["reconciliationFailed"] = False
        patch.status["lastError"] = None

    except Exception as e:
        # Reconciliation logic failed - log clearly and mark CR
        logger.error(f"[{cr_name}] Reconciliation cycle error: {e}", exc_info=True)
        patch.status["lastError"] = f"Reconciliation error: {str(e)[:400]}"
        patch.status["lastErrorTime"] = datetime.now(timezone.utc).isoformat()
        patch.status["reconciliationFailed"] = True
        # Don't re-raise - let operator continue with other CRs


@kopf.on.delete("ppa.example.com", "v1", "predictiveautoscalers")
def on_delete(meta, **kwargs):
    """Clean up per-CR state when a CR is deleted (thread-safe)."""
    key = (meta.get("namespace", NAMESPACE), meta.get("name", "unknown"))
    with _cr_state_lock:
        removed = _cr_state.pop(key, None)
    if removed:
        logger.info(f"Cleaned up state for {key}")
