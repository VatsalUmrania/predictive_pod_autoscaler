# operator/main.py — kopf timer handler (thin orchestrator)
"""PPA Operator: reads Prometheus, predicts with TFLite, scales deployment."""

import logging
import kopf

from config import TARGET_APP, TIMER_INTERVAL, INITIAL_DELAY, STABILIZATION_STEPS
from features import build_feature_vector
from predictor import Predictor
from scaler import calculate_replicas, scale_deployment

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("ppa.operator")

predictor = Predictor()
_stable_count = 0
_last_prediction = 0.0


@kopf.timer(
    "ppa.example.com", "v1", "predictiveautoscalers",
    interval=TIMER_INTERVAL,
    initial_delay=INITIAL_DELAY,
)
def reconcile(spec, status, patch, **kwargs):
    """Main control loop — runs every TIMER_INTERVAL seconds."""
    global _stable_count, _last_prediction

    target = spec.get("targetDeployment", TARGET_APP)

    # 1. Fetch features from Prometheus
    features = build_feature_vector()
    logger.info(
        f"RPS={features['requests_per_second']:.1f}  "
        f"P95={features['latency_p95_ms']:.1f}ms  "
        f"CPU={features['cpu_usage_percent']:.1f}%  "
        f"Replicas={features['current_replicas']:.0f}"
    )

    # 2. Feed into predictor
    predictor.update(features)
    if not predictor.ready():
        logger.info(
            f"Warming up: {len(predictor.history)}/{predictor.history.__class__.__name__} "
            "steps collected"
        )
        return

    # 3. Predict future load
    predicted_load = predictor.predict()
    logger.info(f"Predicted load: {predicted_load:.1f} req/s")

    # 4. Stabilization — only scale if prediction is stable
    if _last_prediction > 0:
        change_pct = abs(predicted_load - _last_prediction) / _last_prediction
        if change_pct < 0.10:
            _stable_count += 1
        else:
            _stable_count = 0

    _last_prediction = predicted_load

    if _stable_count < STABILIZATION_STEPS:
        logger.info(f"Stabilizing: {_stable_count}/{STABILIZATION_STEPS} stable reads")
        return

    # 5. Calculate and apply desired replicas
    current = int(features["current_replicas"])
    desired = calculate_replicas(predicted_load, current)

    if desired != current:
        logger.info(f"Scaling {target}: {current} → {desired}")
        scale_deployment(target, desired)
        patch.status["lastScaleTime"] = __import__("datetime").datetime.utcnow().isoformat()
        _stable_count = 0  # reset after scaling
    else:
        logger.info(f"No scaling needed: {current} replicas is correct")

    patch.status["lastPredictedLoad"] = round(predicted_load, 2)
    patch.status["currentReplicas"] = current
    patch.status["desiredReplicas"] = desired
