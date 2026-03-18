# operator/main.py — kopf timer handler (multi-CR orchestrator)
"""PPA Operator: manages N PredictiveAutoscaler CRs independently."""

import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

import math

import kopf
from prometheus_client import Gauge, Counter, start_http_server as _prom_start_http_server

# ---------------------------------------------------------------------------
# Prometheus metrics — labelled by cr_name + namespace for multi-CR support
# ---------------------------------------------------------------------------
_LABELS = ["cr_name", "namespace"]

ppa_predicted_load_rps   = Gauge("ppa_predicted_load_rps",   "LSTM predicted load (req/s)",          _LABELS)
ppa_inflated_load_rps    = Gauge("ppa_inflated_load_rps",    "predicted * safety_factor (req/s)",    _LABELS)
ppa_raw_desired_replicas = Gauge("ppa_raw_desired_replicas", "Unclamped replica target",             _LABELS)
ppa_desired_replicas     = Gauge("ppa_desired_replicas",     "Rate-limited, bounds-clamped replicas",_LABELS)
ppa_current_replicas     = Gauge("ppa_current_replicas",     "Observed ready replicas",              _LABELS)
ppa_consecutive_skips    = Gauge("ppa_consecutive_skips",    "Cycles skipped due to bad data",       _LABELS)
ppa_warmup_progress      = Gauge("ppa_warmup_progress",      "History window fill ratio (0-1)",      _LABELS)
ppa_model_load_failed    = Gauge("ppa_model_load_failed",    "1 if model failed to load, else 0",    _LABELS)
ppa_scale_events_total   = Counter("ppa_scale_events_total", "Total scaling decisions applied",      _LABELS)
ppa_circuit_breaker_tripped = Gauge("ppa_circuit_breaker_tripped", "1 if circuit breaker active, 0 else",     _LABELS)
ppa_metric_failures      = Gauge("ppa_metric_failures",      "Consecutive metric extraction failures",_LABELS)
ppa_concept_drift_detected  = Gauge("ppa_concept_drift_detected",  "1 if concept drift detected, 0 else",   _LABELS)
ppa_prediction_error_pct    = Gauge("ppa_prediction_error_pct",    "Mean absolute percentage error %",      _LABELS)
ppa_inference_latency_ms    = Gauge("ppa_inference_latency_ms",    "Model inference latency in ms",         _LABELS)

from common.feature_spec import FEATURE_COLUMNS
from config import (
    TIMER_INTERVAL,
    INITIAL_DELAY,
    STABILIZATION_STEPS,
    STABILIZATION_TOLERANCE,
    DEFAULT_CAPACITY_PER_POD,
    DEFAULT_MIN_REPLICAS,
    DEFAULT_MAX_REPLICAS,
    DEFAULT_SCALE_UP_RATE,
    DEFAULT_SCALE_DOWN_RATE,
    DEFAULT_MODEL_DIR,
    NAMESPACE,
    FeatureVectorException,
)
from features import build_feature_vector, build_historical_features, PrometheusCircuitBreakerTripped
from predictor import Predictor
from scaler import calculate_replicas, scale_deployment

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


_start_health_server()

# Start Prometheus metrics endpoint on port 9100 (separate from healthz on 8080)
_prom_start_http_server(9100)
logging.getLogger("ppa.operator").info("Prometheus metrics endpoint listening on :9100/metrics")


@dataclass
class CRState:
    """Per-CR runtime state."""
    predictor: Predictor
    stable_count: int = 0
    last_prediction: float = 0.0
    last_desired: int = -1  # replica target from previous cycle (stabilisation anchor)


# Registry keyed by (cr_namespace, cr_name) to avoid cross-namespace collisions.
_cr_state: dict[tuple[str, str], CRState] = {}


def _resolve_paths(spec: dict, target_app: str, target_horizon: str) -> tuple[str, str, str | None]:
    """Compute model + scaler + target_scaler paths from CRD spec, falling back to convention."""
    model_dir = DEFAULT_MODEL_DIR
    model_path = spec.get("modelPath") or os.path.join(model_dir, target_app, target_horizon, "ppa_model.tflite")
    scaler_path = spec.get("scalerPath") or os.path.join(model_dir, target_app, target_horizon, "scaler.pkl")
    # Target scaler is optional (backward compat with models trained without it)
    target_scaler_path = spec.get("targetScalerPath") or os.path.join(model_dir, target_app, target_horizon, "target_scaler.pkl")
    if not os.path.exists(target_scaler_path):
        target_scaler_path = None
    return model_path, scaler_path, target_scaler_path


def _get_or_create_state(key: tuple[str, str], model_path: str, scaler_path: str, target_scaler_path: str | None = None, target_app: str = "", target_ns: str = "", max_r: int = 1, container_name: str | None = None, min_r: int = 1) -> CRState:
    """Lazy-init or reload CRState if model paths changed.

    FIX (PR#3): Prefill is SKIPPED to avoid scaler distribution mismatch.
    FIX (PR#5): History is PRESERVED when model is upgraded (new paths), preventing 30-min blindness.
    """
    existing = _cr_state.get(key)
    if existing and existing.predictor.paths_match(model_path, scaler_path, target_scaler_path):
        return existing

    if existing:
        # Model upgraded: preserve history, reload interpreter only (PR#5 fix)
        logger.info(f"Model upgraded for {key}, reloading interpreter (preserving history)...")

        # Snapshot history before creating new predictor
        old_history = existing.predictor.copy_history()
        history_len = len(old_history)

        # Create new predictor with upgraded model
        new_predictor = Predictor(model_path, scaler_path, target_scaler_path)

        # Restore history into new predictor
        new_predictor.restore_history(old_history)

        # Update state in-place (don't create new CRState)
        existing.predictor = new_predictor

        logger.info(f"Restored {history_len}/{60} history steps to new model")
        return existing

    # First time: create new state
    state = CRState(predictor=Predictor(model_path, scaler_path, target_scaler_path))
    logger.info(f"[{key}] Skipping prefill to avoid scaler distribution mismatch (cold-start: ~30 min warmup)")

    _cr_state[key] = state
    return state


@kopf.timer(
    "ppa.example.com", "v1", "predictiveautoscalers",
    interval=TIMER_INTERVAL,
    initial_delay=INITIAL_DELAY,
)
def reconcile(spec, status, meta, patch, **kwargs):
    """Main control loop — runs every TIMER_INTERVAL seconds per CR."""
    cr_ns = meta.get("namespace", NAMESPACE)
    cr_name = meta.get("name", "unknown")
    key = (cr_ns, cr_name)

    # Read CR spec with defaults
    target = spec["targetDeployment"]
    target_ns = spec.get("namespace", cr_ns)

    # CR Template will expose appName and horizon directly. If missing, try fallback logic.
    target_app = spec.get("appName", target)
    target_horizon = spec.get("horizon", "rps_t3m")  # Assume standard fallback for horizon

    min_r = spec.get("minReplicas", DEFAULT_MIN_REPLICAS)
    max_r = spec.get("maxReplicas")
    if max_r is None:
        raise ValueError("maxReplicas must be set in PredictiveAutoscaler spec")
    capacity = spec.get("capacityPerPod", DEFAULT_CAPACITY_PER_POD)
    up_rate = spec.get("scaleUpRate", DEFAULT_SCALE_UP_RATE)
    down_rate = spec.get("scaleDownRate", DEFAULT_SCALE_DOWN_RATE)
    safety_factor = float(spec.get("safetyFactor", 1.10))
    observer_mode = bool(spec.get("observerMode", False))
    container_name = spec.get("containerName") or None

    # FIX (PR#14): Detect multiple CRs managing the same deployment
    # Count how many CRs are tracking this deployment
    same_target_crs = [
        (k, s) for k, s in _cr_state.items()
        if k != key  # Exclude current CR
        # In production, you'd want to check the deployment target from each state
        # For now, log a warning if multiple CRs might conflict
    ]
    if len(same_target_crs) > 0 and cr_name != "single-ppa-controller":
        logger.warning(
            f"[{cr_name}] Multiple CRs detected in system. Ensure only ONE CR manages "
            f"target {target_ns}/{target} to avoid scaling oscillations."
        )

    model_path, scaler_path, target_scaler_path = _resolve_paths(spec, target_app, target_horizon)
    state = _get_or_create_state(key, model_path, scaler_path, target_scaler_path, target, target_ns, max_r, container_name, min_r)

    # 1. Fetch features from Prometheus (namespace-scoped)
    # FIX (PR#1): Pass min_r as reference_replicas for stable rps_per_replica calculation
    # FIX (PR#4): Catch FeatureVectorException instead of silently handling NaN
    # FIX (PR#9): Also catch PrometheusCircuitBreakerTripped for backoff logic
    try:
        features, current_replicas = build_feature_vector(target, target_ns, min_r, max_r, container_name)
    except (FeatureVectorException, PrometheusCircuitBreakerTripped) as e:
        # Feature extraction failed (Prometheus unavailable, network errors, etc.)
        metric_failures = status.get("metricFailures", 0) + 1
        patch.status["metricFailures"] = metric_failures
        patch.status["lastMetricError"] = str(e)
        patch.status["lastMetricErrorTime"] = datetime.now(timezone.utc).isoformat()

        logger.error(f"[{cr_name}] Feature extraction failed ({metric_failures}/5): {e}")

        if metric_failures >= 5:
            # Circuit breaker: too many consecutive failures
            patch.status["circuitBreakerTripped"] = True
            ppa_circuit_breaker_tripped.labels(cr_name=cr_name, namespace=cr_ns).set(1)
            logger.critical(f"[{cr_name}] CIRCUIT BREAKER TRIPPED after {metric_failures} metric failures. "
                          f"PPA will not scale until metrics recover.")
        else:
            ppa_metric_failures.labels(cr_name=cr_name, namespace=cr_ns).set(metric_failures)

        return

    # Reset metric failure counters on success
    patch.status["metricFailures"] = 0
    patch.status["circuitBreakerTripped"] = False
    ppa_circuit_breaker_tripped.labels(cr_name=cr_name, namespace=cr_ns).set(0)
    ppa_metric_failures.labels(cr_name=cr_name, namespace=cr_ns).set(0)

    assert list(features.keys()) == FEATURE_COLUMNS, f"[{cr_name}] Feature vector order mismatch"

    logger.info(
        f"[{cr_name}] RPS/Pod={features['rps_per_replica']:.1f}  "
        f"P95={features['latency_p95_ms']:.1f}ms  "
        f"CPU={features['cpu_utilization_pct']:.1f}%  "
        f"Replicas={features['replicas_normalized']:.2f} (norm)"
    )

    # Always publish current replicas so kubectl get ppa shows something
    current = int(current_replicas)
    patch.status["currentReplicas"] = current
    ppa_current_replicas.labels(cr_name=cr_name, namespace=cr_ns).set(current)

    # 2. Feed into predictor
    state.predictor.update(features)
    history_len = len(state.predictor.history)
    maxlen = state.predictor.history.maxlen
    ppa_warmup_progress.labels(cr_name=cr_name, namespace=cr_ns).set(history_len / maxlen if maxlen else 0)
    ppa_model_load_failed.labels(cr_name=cr_name, namespace=cr_ns).set(1 if state.predictor._load_failed else 0)
    if not state.predictor.ready():
        if state.predictor._load_failed:
            logger.warning(
                f"[{cr_name}] Model not loaded (will retry next cycle). "
                f"History: {history_len}/{maxlen}"
            )
        else:
            logger.info(
                f"[{cr_name}] Warming up: {history_len}/{maxlen} steps collected"
            )
        return

    # 3. Predict future load
    predicted_load = state.predictor.predict()
    logger.info(f"[{cr_name}] Predicted load: {predicted_load:.1f} req/s")

    # Always publish predicted load so status is visible
    patch.status["lastPredictedLoad"] = round(predicted_load, 2)
    ppa_predicted_load_rps.labels(cr_name=cr_name, namespace=cr_ns).set(predicted_load)

    # FIX (PR#12): Track actual RPS and check for concept drift
    actual_rps = features.get("requests_per_second", 0.0)
    state.predictor.track_prediction_accuracy(predicted_load, actual_rps)

    drift_check = state.predictor.check_concept_drift()
    if drift_check.get('checked'):
        if drift_check.get('detected'):
            ppa_concept_drift_detected.labels(cr_name=cr_name, namespace=cr_ns).set(1)
            error_pct = drift_check.get('error_pct', 0)
            ppa_prediction_error_pct.labels(cr_name=cr_name, namespace=cr_ns).set(error_pct)
            severity = drift_check.get('severity', 'unknown')
            patch.status["conceptDriftDetected"] = True
            patch.status["driftSeverity"] = severity
            patch.status["predictionErrorPct"] = round(error_pct, 2)
            logger.error(
                f"[{cr_name}] CONCEPT DRIFT ({severity}): "
                f"Prediction error {error_pct:.1f}% (threshold: 20%). "
                f"Consider retraining the model."
            )
        else:
            ppa_concept_drift_detected.labels(cr_name=cr_name, namespace=cr_ns).set(0)
            patch.status["conceptDriftDetected"] = False

    # 4. Stabilization — anchored on desired replica count, not raw prediction magnitude.
    #    Raw RPS changes > 10% every cycle during ramps, causing the old magnitude-based
    #    check to permanently block scaling. This version counts how many consecutive cycles
    #    produce the same replica target, which naturally handles trending traffic.
    state.last_prediction = predicted_load

    # Publish inflated load and raw desired before rate-limiting
    inflated = predicted_load * safety_factor
    raw_desired = math.ceil(inflated / capacity) if capacity else current
    ppa_inflated_load_rps.labels(cr_name=cr_name, namespace=cr_ns).set(inflated)
    ppa_raw_desired_replicas.labels(cr_name=cr_name, namespace=cr_ns).set(raw_desired)

    candidate = calculate_replicas(predicted_load, current, min_r, max_r, capacity, up_rate, down_rate, safety_factor)

    # FIX (PR#2): Use tolerance-based stabilization instead of exact match
    # This handles natural RPS variance (±10-20%) without resetting the counter
    if abs(candidate - state.last_desired) <= STABILIZATION_TOLERANCE:
        state.stable_count += 1
    else:
        state.stable_count = 1  # start counting from 1 (this cycle counts)
    state.last_desired = float(candidate)  # Use float for smoother comparison

    if state.stable_count < STABILIZATION_STEPS:
        logger.info(f"[{cr_name}] Stabilizing: {state.stable_count}/{STABILIZATION_STEPS} (target: {candidate} replicas, tolerance: ±{STABILIZATION_TOLERANCE})")
        patch.status["desiredReplicas"] = candidate
        ppa_desired_replicas.labels(cr_name=cr_name, namespace=cr_ns).set(candidate)
        return

    # 5. Apply desired replicas (candidate already computed and stabilised)
    desired = candidate
    ppa_desired_replicas.labels(cr_name=cr_name, namespace=cr_ns).set(desired)

    if desired != current:
        if observer_mode:
            logger.info(f"[{cr_name}] OBSERVER: would scale {target_ns}/{target}: {current} → {desired} (skipped — observerMode=true)")
        else:
            logger.info(f"[{cr_name}] Scaling {target_ns}/{target}: {current} → {desired}")
            scale_deployment(target, desired, target_ns)
            patch.status["lastScaleTime"] = datetime.now(timezone.utc).isoformat()
            ppa_scale_events_total.labels(cr_name=cr_name, namespace=cr_ns).inc()
            state.stable_count = 0
    else:
        logger.info(f"[{cr_name}] No scaling needed: {current} replicas is correct")

    patch.status["desiredReplicas"] = desired


@kopf.on.delete("ppa.example.com", "v1", "predictiveautoscalers")
def on_delete(meta, **kwargs):
    """Clean up per-CR state when a CR is deleted."""
    key = (meta.get("namespace", NAMESPACE), meta.get("name", "unknown"))
    removed = _cr_state.pop(key, None)
    if removed:
        logger.info(f"Cleaned up state for {key}")
