# operator/main.py — kopf timer handler (multi-CR orchestrator)
"""PPA Operator: manages N PredictiveAutoscaler CRs independently."""

import logging
import os
from dataclasses import dataclass

import math

import kopf

from common.feature_spec import FEATURE_COLUMNS
from config import (
    TIMER_INTERVAL,
    INITIAL_DELAY,
    STABILIZATION_STEPS,
    DEFAULT_CAPACITY_PER_POD,
    DEFAULT_MIN_REPLICAS,
    DEFAULT_MAX_REPLICAS,
    DEFAULT_SCALE_UP_RATE,
    DEFAULT_SCALE_DOWN_RATE,
    DEFAULT_MODEL_DIR,
    NAMESPACE,
)
from features import build_feature_vector
from predictor import Predictor
from scaler import calculate_replicas, scale_deployment

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("ppa.operator")


@dataclass
class CRState:
    """Per-CR runtime state."""
    predictor: Predictor
    stable_count: int = 0
    last_prediction: float = 0.0


# Registry keyed by (cr_namespace, cr_name) to avoid cross-namespace collisions.
_cr_state: dict[tuple[str, str], CRState] = {}


def _resolve_paths(spec: dict, target: str) -> tuple[str, str]:
    """Compute model + scaler paths from CRD spec, falling back to convention."""
    model_dir = DEFAULT_MODEL_DIR
    model_path = spec.get("modelPath") or os.path.join(model_dir, target, "ppa_model.tflite")
    scaler_path = spec.get("scalerPath") or os.path.join(model_dir, target, "scaler.pkl")
    return model_path, scaler_path


def _get_or_create_state(key: tuple[str, str], model_path: str, scaler_path: str) -> CRState:
    """Lazy-init or reload CRState if model paths changed."""
    existing = _cr_state.get(key)
    if existing and existing.predictor.paths_match(model_path, scaler_path):
        return existing

    if existing:
        logger.info(f"Model paths changed for {key}, reloading predictor...")

    state = CRState(predictor=Predictor(model_path, scaler_path))
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
    min_r = spec.get("minReplicas", DEFAULT_MIN_REPLICAS)
    max_r = spec.get("maxReplicas")
    if max_r is None:
        raise ValueError("maxReplicas must be set in PredictiveAutoscaler spec")
    capacity = spec.get("capacityPerPod", DEFAULT_CAPACITY_PER_POD)
    up_rate = spec.get("scaleUpRate", DEFAULT_SCALE_UP_RATE)
    down_rate = spec.get("scaleDownRate", DEFAULT_SCALE_DOWN_RATE)

    model_path, scaler_path = _resolve_paths(spec, target)
    state = _get_or_create_state(key, model_path, scaler_path)

    # 1. Fetch features from Prometheus (namespace-scoped)
    features, current_replicas = build_feature_vector(target, target_ns, max_r)

    if math.isnan(features.get("cpu_utilization_pct", float('nan'))):
        logger.warning(f"[{cr_name}] cpu_utilization_pct is NaN, delegating to HPA")
        return
    if math.isnan(features.get("memory_utilization_pct", float('nan'))):
        logger.warning(f"[{cr_name}] memory_utilization_pct is NaN, delegating to HPA")
        return
    if current_replicas == 0 or math.isnan(current_replicas):
        logger.warning(f"[{cr_name}] current_replicas is 0 or NaN, skipping cycle")
        return

    assert list(features.keys()) == FEATURE_COLUMNS, f"[{cr_name}] Feature vector order mismatch"

    logger.info(
        f"[{cr_name}] RPS/Pod={features['rps_per_replica']:.1f}  "
        f"P95={features['latency_p95_ms']:.1f}ms  "
        f"CPU={features['cpu_utilization_pct']:.1f}%  "
        f"Replicas={features['replicas_normalized']:.2f} (norm)"
    )

    # 2. Feed into predictor
    state.predictor.update(features)
    if not state.predictor.ready():
        logger.info(
            f"[{cr_name}] Warming up: {len(state.predictor.history)}/{state.predictor.history.maxlen} "
            "steps collected"
        )
        return

    # 3. Predict future load
    predicted_load = state.predictor.predict()
    logger.info(f"[{cr_name}] Predicted load: {predicted_load:.1f} req/s")

    # 4. Stabilization
    if state.last_prediction > 0:
        change_pct = abs(predicted_load - state.last_prediction) / state.last_prediction
        if change_pct < 0.10:
            state.stable_count += 1
        else:
            state.stable_count = 0

    state.last_prediction = predicted_load

    if state.stable_count < STABILIZATION_STEPS:
        logger.info(f"[{cr_name}] Stabilizing: {state.stable_count}/{STABILIZATION_STEPS} stable reads")
        return

    # 5. Calculate and apply desired replicas
    current = int(current_replicas)
    desired = calculate_replicas(predicted_load, current, min_r, max_r, capacity, up_rate, down_rate)

    if desired != current:
        logger.info(f"[{cr_name}] Scaling {target_ns}/{target}: {current} → {desired}")
        scale_deployment(target, desired, target_ns)
        patch.status["lastScaleTime"] = __import__("datetime").datetime.utcnow().isoformat()
        state.stable_count = 0
    else:
        logger.info(f"[{cr_name}] No scaling needed: {current} replicas is correct")

    patch.status["lastPredictedLoad"] = round(predicted_load, 2)
    patch.status["currentReplicas"] = current
    patch.status["desiredReplicas"] = desired


@kopf.on.delete("ppa.example.com", "v1", "predictiveautoscalers")
def on_delete(meta, **kwargs):
    """Clean up per-CR state when a CR is deleted."""
    key = (meta.get("namespace", NAMESPACE), meta.get("name", "unknown"))
    removed = _cr_state.pop(key, None)
    if removed:
        logger.info(f"Cleaned up state for {key}")
