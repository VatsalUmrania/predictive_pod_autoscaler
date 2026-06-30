# operator/state_machine.py — ScalerStateMachine class
"""State machine for PPA reconciliation cycle.

Extracts the reconciliation logic from main.py into a cohesive class
with clear predict(), decide(), and act() phases.
"""

import asyncio
import hashlib
import logging
import math
import os
import random
import statistics
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

import kopf

from ppa.bus.nats_client import PpaNATSClient
from ppa.bus.prediction_event import PpaPredictionEvent
from ppa.config import (
    DEFAULT_CAPACITY_PER_POD,
    FEATURE_COLUMNS,
    STABILIZATION_STEPS,
    STABILIZATION_TOLERANCE,
    FeatureVectorException,
)
from ppa.domain import CRState, calculate_replicas
from ppa.operator.features import PrometheusCircuitBreakerTripped, build_feature_vector
from ppa.operator.metrics import PpaOperatorMetrics
from ppa.operator.scaler import scale_deployment

logger = logging.getLogger("ppa.operator.state_machine")

# Multi-model prediction registry (shared with main.py)
_prediction_registry: dict[tuple[str, str], dict[str, float]] = {}
_prediction_registry_lock = threading.Lock()
_PREDICTION_TTL_SECONDS = 60  # Updated from TIMER_INTERVAL * 2

# Decision trace sampling
DECISION_TRACE_SAMPLE_RATE = 0.10


def _trace_sampled(
    event_key: str, sample_rate: float = DECISION_TRACE_SAMPLE_RATE
) -> bool:
    """Check if this event should be traced (sampled)."""
    digest = hashlib.sha256(event_key.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) / 0xFFFFFFFF
    return bucket < sample_rate


def _update_status(patch: kopf.Patch, key: str, value: Any) -> None:
    """Idempotent status update: only write if value changed."""
    if patch.status.get(key) != value:
        patch.status[key] = value


def _record_scale_failure(state: CRState, patch: kopf.Patch, reason: str) -> None:
    """Record a scaling failure and calculate backoff."""
    scale_backoff_initial_seconds = 30.0
    scale_backoff_max_seconds = 300.0
    scale_backoff_jitter = 0.2

    state.scale_failures += 1
    state.last_scale_failure_time = time.time()
    base = min(
        scale_backoff_max_seconds,
        scale_backoff_initial_seconds * (2 ** max(state.scale_failures - 1, 0)),
    )
    state.next_scale_retry_time = state.last_scale_failure_time + base * random.uniform(
        1.0 - scale_backoff_jitter, 1.0 + scale_backoff_jitter
    )
    state.last_scale_error = reason
    _update_status(patch, "lastScaleError", reason)
    _update_status(patch, "scaleBackoffUntil", state.next_scale_retry_time)


def _reset_scale_backoff(state: CRState, patch: kopf.Patch) -> None:
    """Reset scaling failure backoff state."""
    state.scale_failures = 0
    state.last_scale_failure_time = 0.0
    state.next_scale_retry_time = 0.0
    state.last_scale_error = None
    _update_status(patch, "lastScaleError", None)
    _update_status(patch, "scaleBackoffUntil", None)


def _validate_input_signals(features: dict[str, Any]) -> bool:
    """Return False when ALL primary signals are absent/zero."""
    rps = features.get("normalized_rps", 0.0) or 0.0
    cpu = features.get("cpu_utilization_pct", 0.0) or 0.0
    lat = features.get("latency_normalized")
    lat_is_nan = lat is None or (isinstance(lat, float) and math.isnan(lat))

    if rps <= 0.0 and cpu <= 0.0 and lat_is_nan:
        return False
    return True


def _check_prediction_sanity(predicted_load: float, features: dict[str, Any]) -> bool:
    """Return False when predicted_load is high but inputs are near-zero."""
    rps = features.get("normalized_rps", 0.0) or 0.0
    if predicted_load > 1.5 and rps <= 0.0:  # v3: threshold is on normalized scale
        return False
    return True


def _trace_value(value: Any) -> float | int | str | None:
    """Convert value to trace-safe format."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return "nan"
        return round(value, 4)
    return str(value)


def _compact_feature_trace(
    features: dict[str, Any],
) -> dict[str, float | int | str | None]:
    """Compact feature vector for tracing."""
    return {
        name: _trace_value(features.get(name))
        for name in FEATURE_COLUMNS
        if name in features
    }


def _maybe_record_decision_trace(
    cr_name: str,
    cr_ns: str,
    config: dict[str, Any],
    state: CRState,
    patch: kopf.Patch,
    features: dict[str, Any],
    raw_metrics: dict[str, float],
    predicted_load: float,
    candidate: int,
    final_decision: int,
    current: int,
    reason: str,
    apply_scaling: bool,
    force: bool = False,
) -> None:
    """Record decision trace for debugging."""
    degraded_reasons = state.degraded_reasons or []
    force = force or bool(degraded_reasons) or final_decision != current

    now = time.time()
    event_key = (
        f"{cr_ns}/{cr_name}:{config['target_horizon']}:{state.active_model_version}:"
        f"{int(now // max(30, 1))}:{current}:{final_decision}:"
        f"{round(predicted_load, 1)}:{reason}"
    )

    if not force and not _trace_sampled(event_key):
        return

    trace = {
        "eventKey": event_key,
        "modelVersion": state.active_model_version,
        "horizon": config["target_horizon"],
        "currentReplicas": current,
        "inputFeatures": _compact_feature_trace(features),
        "rawMetrics": {key: _trace_value(value) for key, value in raw_metrics.items()},
        "prediction": _trace_value(predicted_load),
        "candidateReplicas": candidate,
        "finalDecision": final_decision,
        "applyScaling": apply_scaling,
        "reason": reason,
        "degradedReasons": degraded_reasons,
    }
    state.last_decision_trace = trace
    _update_status(patch, "decisionTrace", trace)
    logger.info(f"[{cr_name}] decision trace: {trace}")


class ScalerStateMachine:
    """Manages the full reconciliation lifecycle for a PredictiveAutoscaler CR.

    The reconciliation cycle has four phases:
    1. fetch_features: Get current metrics from Prometheus
    2. update_predictor: Add to history, check warmup
    3. decide: Predict load, calculate target replicas
    4. act: Apply scaling to Kubernetes
    """

    def __init__(
        self,
        cr_name: str,
        cr_namespace: str,
        state: CRState,
        config: dict[str, Any],
        patch: kopf.Patch,
        status: dict[str, Any] | None,
    ):
        self.cr_name = cr_name
        self.cr_namespace = cr_namespace
        self.state = state
        self.config = config
        self.patch = patch
        self.status = status
        # Resolve NATS URL — deployment sets NATS_URL; fall back to localhost for local dev.
        self._nats = PpaNATSClient(
            nats_url=os.getenv("NATS_URL", "nats://localhost:4222")
        )
        self._pending_events: deque = deque(maxlen=100)

        # Dedicated event loop for NATS coroutines.
        # kopf reconcile handlers run in ThreadPoolExecutor threads that have
        # no event loop, so asyncio.get_event_loop() raises RuntimeError in
        # Python 3.10+.  We keep one loop per CR alive in a daemon thread and
        # schedule coroutines onto it with run_coroutine_threadsafe.
        self._nats_loop = asyncio.new_event_loop()
        self._nats_thread = threading.Thread(
            target=self._nats_loop.run_forever,
            daemon=True,
            name=f"ppa-nats-{cr_name}",
        )
        self._nats_thread.start()

    def fetch_and_validate_features(
        self,
    ) -> tuple[dict[str, Any], int, dict[str, float], list[str], bool]:
        """Fetch features from Prometheus and handle failures.

        Returns:
            Tuple of (features, current_replicas, raw_metrics, degraded_reasons, should_continue)
        """
        try:
            features, current_replicas, raw_metrics, degraded_reasons = (
                build_feature_vector(
                    self.config["target"],
                    self.config["target_ns"],
                    self.config["min_r"],
                    self.config["max_r"],
                    self.config["container_name"],
                    self.config["prom_url"],
                    self.state,
                )
            )
            self.state.last_successful_cycle = time.time()
            self.state.consecutive_failures = 0
            self.state.last_known_good_replicas = int(current_replicas)
            return features, int(current_replicas), raw_metrics, degraded_reasons, True

        except (FeatureVectorException, PrometheusCircuitBreakerTripped) as e:
            self.state.consecutive_failures += 1
            metric_failures = (
                self.status.get("metricFailures", 0) if self.status else 0
            ) + 1

            logger.error(
                f"[{self.cr_name}] Feature extraction failed ({metric_failures}/5): {e}"
            )

            # Graceful degradation: use last known good state
            if (
                self.state.last_known_good_replicas > 0
                and self.state.consecutive_failures <= 3
            ):
                fallback_replicas = self.state.last_known_good_replicas
                logger.warning(
                    f"[{self.cr_name}] Using fallback scaling: {fallback_replicas} replicas "
                    f"(last known good, failure {self.state.consecutive_failures}/3)"
                )

                if not self.config["observer_mode"]:
                    logger.info(
                        f"[{self.cr_name}] FALLBACK: Scaling {self.config['target_ns']}/{self.config['target']} "
                        f"to {fallback_replicas} replicas"
                    )
                    if not scale_deployment(
                        self.config["target"],
                        fallback_replicas,
                        self.config["target_ns"],
                    ):
                        _record_scale_failure(
                            self.state, self.patch, "fallback scale patch failed"
                        )

            return {}, 0, {}, [], False

    def update_predictor_and_check_ready(
        self, features: dict[str, Any], current: int
    ) -> bool:
        """Update predictor history and check readiness.

        Returns:
            True if predictor is ready for prediction, False otherwise
        """
        if self.state.predictor is None:
            if not self.state.predictor_missing_logged:
                logger.debug(
                    f"[{self.cr_name}] Predictor not available yet. Waiting for artifacts to load..."
                )
                self.state.predictor_missing_logged = True
            _update_status(self.patch, "currentReplicas", current)
            return False

        self.state.predictor.update(features)
        history_len = len(self.state.predictor.history)
        maxlen = self.state.predictor.history.maxlen

        # Update Prometheus metrics (centralized in metrics.py)
        metrics = PpaOperatorMetrics.for_cr(self.cr_name, self.cr_namespace)
        metrics.warmup_progress.set(history_len / maxlen if maxlen else 0)
        metrics.model_load_failed.set(1 if self.state.predictor._load_failed else 0)

        _update_status(self.patch, "currentReplicas", current)

        if not self.state.predictor.ready():
            if self.state.predictor._load_failed:
                logger.debug(
                    f"[{self.cr_name}] Model not loaded (will retry next cycle). "
                    f"History: {history_len}/{maxlen}"
                )
            else:
                logger.info(
                    f"[{self.cr_name}] Warming up: {history_len}/{maxlen} steps collected"
                )
            return False

        # Reset transition flag when predictor becomes ready
        self.state.predictor_missing_logged = False

        lat = features.get("latency_normalized", float("nan"))
        lat_display = (
            f"{lat:.2f}" if isinstance(lat, float) and not math.isnan(lat) else "nan"
        )
        logger.info(
            f"[{self.cr_name}] normalized_rps={features['normalized_rps']:.3f} "
            f"P95_norm={lat_display} "
            f"CPU={features['cpu_utilization_pct']:.1f}% "
        )

        # Input validation: skip prediction when all primary signals are dead
        if not _validate_input_signals(features):
            logger.warning(
                f"[{self.cr_name}] Skipping prediction due to invalid input signals "
                f"(rps=0, cpu=0, latency=NaN)"
            )
            return False

        return True

    def predict(
        self, features: dict[str, Any], raw_metrics: dict[str, float] | None = None
    ) -> float:
        """Run LSTM inference to get predicted load in req/s.

        The foundation model outputs a normalized ratio in [0, 1].  We
        recover the original rolling_max_rps denominator from raw_metrics
        and pass it to Predictor.predict() for correct denormalization:
            predicted_rps = model_output_ratio × rolling_max_rps
        """
        predictor = self.state.predictor
        if predictor is None:
            return 0.0

        # Recover rolling_max_rps: it was the normalization denominator during
        # both training (export_training_data.py, floor=1.0) and inference
        # (features.py, floor=1.0).  Back-computed from raw_rps / normalized_rps.
        raw_rps: float = float(
            (raw_metrics or {}).get("requests_per_second", 0.0) or 0.0
        )
        norm_rps: float = float(features.get("normalized_rps", 0.0) or 0.0)
        if norm_rps > 0.0:
            rolling_max_rps = raw_rps / norm_rps  # exact inverse of normalization
        else:
            rolling_max_rps = max(raw_rps, 1.0)  # floor matches training floor

        predicted_load = predictor.predict(rolling_max_rps=rolling_max_rps)
        logger.info(f"[{self.cr_name}] Predicted load: {predicted_load:.1f} req/s")
        return predicted_load

    def _compute_confidence(self) -> float:
        """Compute prediction confidence from predictor metadata."""
        predictor = self.state.predictor
        if predictor is None:
            return 0.5

        drift_check = predictor.check_concept_drift()
        if drift_check.get("detected"):
            severity = drift_check.get("severity", "low")
            return {"low": 0.6, "medium": 0.4, "high": 0.2}.get(severity, 0.5)

        return 0.75

    async def _publish_prediction(
        self, predicted_rps: float, features: dict[str, Any]
    ) -> None:
        """Publish prediction event to NATS for NEXUS Prescaler consumption.

        Publishes to subject ``ppa.predictions.{deployment}``.
        On NATS failure, accumulates events in _pending_events (max 100).
        On next successful publish, flushes the pending queue.
        """
        raw_features: dict[str, float] = {}
        for k, v in features.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                raw_features[k] = float(v)

        event = PpaPredictionEvent(
            deployment=self.config["target"],
            namespace=self.config["target_ns"],
            predicted_rps=predicted_rps,
            current_rps=features.get("normalized_rps", 0.0) or 0.0,
            confidence=self._compute_confidence(),
            horizon_minutes=self.config["target_horizon"],
            model_version=self.state.active_model_version or "unknown",
            raw_features=raw_features,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        subject = f"ppa.predictions.{self.config['target']}"

        # Lazy connect
        if not self._nats.is_connected:
            try:
                await self._nats.connect()
            except Exception as exc:
                logger.warning(
                    f"[{self.cr_name}] NATS connect failed: {exc} — "
                    f"accumulating event (pending={len(self._pending_events)}/100)"
                )
                self._pending_events.append(event)
                return

        try:
            await self._nats.publish(subject, event.to_dict())
            logger.debug(f"[{self.cr_name}] Published prediction to {subject}")
        except Exception:
            logger.warning(
                f"[{self.cr_name}] NATS publish failed — "
                f"accumulating event (pending={len(self._pending_events)}/100)"
            )
            if len(self._pending_events) >= 100:
                logger.error(
                    f"[{self.cr_name}] Pending event deque overflow — dropping oldest"
                )
                self._pending_events.popleft()
            self._pending_events.append(event)
            return

        # Flush pending events on successful publish
        while self._pending_events:
            pending = self._pending_events.popleft()
            try:
                await self._nats.publish(
                    f"ppa.predictions.{pending.deployment}",
                    pending.to_dict(),
                )
            except Exception:
                self._pending_events.appendleft(pending)
                logger.warning(
                    f"[{self.cr_name}] Pending flush failed — retry next cycle"
                )
                break

    def decide(
        self,
        predicted_load: float,
        features: dict[str, Any],
        raw_metrics: dict[str, float],
        current: int,
    ) -> tuple[int, bool]:
        """Make scaling decision based on predicted load.

        Returns:
            Tuple of (desired_replicas, should_apply_scaling)
        """
        predictor = self.state.predictor
        if predictor is None:
            logger.debug(f"[{self.cr_name}] Skipping scaling (predictor not ready yet)")
            return current, False

        # Prediction sanity check: reject hallucinations
        if not _check_prediction_sanity(predicted_load, features):
            logger.warning(
                f"[{self.cr_name}] Rejecting prediction: inconsistent with input signals "
                f"(predicted={predicted_load:.2f} but normalized_rps=0)"
            )
            _maybe_record_decision_trace(
                self.cr_name,
                self.cr_namespace,
                self.config,
                self.state,
                self.patch,
                features,
                raw_metrics,
                predicted_load,
                current,
                current,
                current,
                "prediction_sanity_reject",
                False,
                force=True,
            )
            return current, False

        # Track prediction accuracy and check for concept drift
        actual_rps = raw_metrics.get("requests_per_second", 0.0)
        predictor.track_prediction_accuracy(predicted_load, actual_rps)

        drift_check = predictor.check_concept_drift()
        if drift_check.get("checked") and drift_check.get("detected"):
            error_pct = drift_check.get("error_pct", 0)
            severity = drift_check.get("severity", "unknown")
            logger.error(
                f"[{self.cr_name}] CONCEPT DRIFT ({severity}): "
                f"Prediction error {error_pct:.1f}% (threshold: 20%). "
                f"Consider retraining the model."
            )

        # Calculate candidate replicas with safety factor
        self.state.last_prediction = predicted_load
        (
            self.config["capacity"]
            if self.config["capacity"] is not None
            else DEFAULT_CAPACITY_PER_POD
        )
        predicted_load * self.config["safety_factor"]

        candidate = calculate_replicas(
            predicted_load,
            current,
            self.config["min_r"],
            self.config["max_r"],
            self.config["capacity"],
            self.config["up_rate"],
            self.config["down_rate"],
            self.config["safety_factor"],
        )

        # Write prediction into shared registry
        deploy_key = (self.config["target_ns"], self.config["target"])
        horizon = self.config["target_horizon"]
        time.time()
        with _prediction_registry_lock:
            if deploy_key not in _prediction_registry:
                _prediction_registry[deploy_key] = {}
            _prediction_registry[deploy_key][horizon] = float(candidate)

        # Tolerance-based stabilization
        if abs(candidate - self.state.last_desired) <= STABILIZATION_TOLERANCE:
            self.state.stable_count += 1
        else:
            self.state.stable_count = 1

        self.state.last_desired = float(candidate)

        if self.state.stable_count < STABILIZATION_STEPS:
            logger.info(
                f"[{self.cr_name}] Stabilizing: {self.state.stable_count}/{STABILIZATION_STEPS} "
                f"(target: {candidate} replicas, tolerance: ±{STABILIZATION_TOLERANCE})"
            )
            _maybe_record_decision_trace(
                self.cr_name,
                self.cr_namespace,
                self.config,
                self.state,
                self.patch,
                features,
                raw_metrics,
                predicted_load,
                candidate,
                candidate,
                current,
                "stabilizing",
                False,
            )
            return candidate, False

        # Multi-model aggregation (active scaler only)
        if not self.config["observer_mode"]:
            valid_predictions: list[float] = []
            with _prediction_registry_lock:
                registry = _prediction_registry.get(deploy_key, {})
                for _h, val in registry.items():
                    valid_predictions.append(val)

            if len(valid_predictions) >= 2:
                pred_max = max(valid_predictions)
                pred_min = min(valid_predictions)
                epsilon = 1e-6
                ratio = pred_max / max(pred_min, epsilon)

                if ratio > 5.0:
                    logger.warning(
                        f"[{self.cr_name}] Skipping scaling due to model disagreement "
                        f"(max={pred_max:.1f}, min={pred_min:.1f}, ratio={ratio:.1f}x > 5)"
                    )
                    _maybe_record_decision_trace(
                        self.cr_name,
                        self.cr_namespace,
                        self.config,
                        self.state,
                        self.patch,
                        features,
                        raw_metrics,
                        predicted_load,
                        candidate,
                        current,
                        current,
                        "model_disagreement",
                        False,
                        force=True,
                    )
                    return current, False

                median_replicas = int(round(statistics.median(valid_predictions)))
                median_replicas = max(
                    self.config["min_r"], min(self.config["max_r"], median_replicas)
                )
                logger.info(
                    f"[{self.cr_name}] Aggregated replica target (median of {len(valid_predictions)} models): "
                    f"{median_replicas}"
                )
                _maybe_record_decision_trace(
                    self.cr_name,
                    self.cr_namespace,
                    self.config,
                    self.state,
                    self.patch,
                    features,
                    raw_metrics,
                    predicted_load,
                    candidate,
                    median_replicas,
                    current,
                    "aggregated_median",
                    True,
                )
                return median_replicas, True

        _maybe_record_decision_trace(
            self.cr_name,
            self.cr_namespace,
            self.config,
            self.state,
            self.patch,
            features,
            raw_metrics,
            predicted_load,
            candidate,
            candidate,
            current,
            "single_model_candidate",
            True,
        )
        return candidate, True

    def _schedule_nats_publish(
        self, predicted_rps: float, features: dict[str, Any]
    ) -> None:
        """Schedule a NATS prediction event on the dedicated background loop.

        Thread-safe: safe to call from any kopf ThreadPoolExecutor thread.
        """
        asyncio.run_coroutine_threadsafe(
            self._publish_prediction(predicted_rps, features),
            self._nats_loop,
        )

    def act(
        self,
        desired: int,
        current: int,
        features: dict[str, Any] | None = None,
        predicted_rps: float | None = None,
    ) -> None:
        """Apply scaling decision to deployment and publish NATS event."""
        _features = features or {}
        _rps = (
            predicted_rps if predicted_rps is not None else self.state.last_prediction
        )

        if desired != current:
            if self.config["observer_mode"]:
                logger.info(
                    f"[{self.cr_name}] OBSERVER: would scale {self.config['target_ns']}/{self.config['target']}: "
                    f"{current} → {desired} (skipped — observerMode=true)"
                )
                # Publish prediction to NATS even in observer mode so NEXUS can
                # record shadow decisions.
                self._schedule_nats_publish(_rps, _features)
            else:
                now = time.time()
                if (
                    self.state.next_scale_retry_time
                    and now < self.state.next_scale_retry_time
                ):
                    logger.warning(
                        f"[{self.cr_name}] Skipping scale attempt during backoff until "
                        f"{self.state.next_scale_retry_time:.0f}"
                    )
                    _update_status(
                        self.patch,
                        "scaleBackoffUntil",
                        self.state.next_scale_retry_time,
                    )
                    return

                logger.info(
                    f"[{self.cr_name}] Scaling {self.config['target_ns']}/{self.config['target']}: "
                    f"{current} → {desired}"
                )
                if scale_deployment(
                    self.config["target"], desired, self.config["target_ns"]
                ):
                    # Increment scale events counter (centralized in metrics.py)
                    metrics = PpaOperatorMetrics.for_cr(self.cr_name, self.cr_namespace)
                    metrics.scale_events.inc()
                    self.state.stable_count = 0
                    _reset_scale_backoff(self.state, self.patch)
                    _update_status(
                        self.patch,
                        "lastScaleTime",
                        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    )
                    # Publish to NATS after successful scale so NEXUS can track outcomes.
                    self._schedule_nats_publish(_rps, _features)
                else:
                    _record_scale_failure(
                        self.state, self.patch, "Kubernetes scale patch failed"
                    )
        else:
            logger.info(
                f"[{self.cr_name}] No scaling needed: {current} replicas is correct"
            )

    def reconcile(self) -> dict[str, Any] | None:
        """Execute full reconciliation cycle: fetch → predict → decide → act.

        Returns:
            Status dict fragment for throttled update, or None if skipped
        """
        # 1. Fetch features
        features, current, raw_metrics, degraded_reasons, should_continue = (
            self.fetch_and_validate_features()
        )
        if not should_continue:
            return None

        # 2. Update predictor and check readiness
        ready = self.update_predictor_and_check_ready(features, current)
        if not ready:
            return None

        # 3. Predict
        predicted_load = self.predict(features, raw_metrics=raw_metrics)

        # 4. Decide
        desired, should_apply = self.decide(
            predicted_load, features, raw_metrics, current
        )

        # 5. Act (pass features + predicted_load so NATS events carry real data)
        if should_apply:
            self.act(desired, current, features=features, predicted_rps=predicted_load)

        # Return status update fragment
        return {
            "currentReplicas": current,
            "desiredReplicas": desired,
            "lastPredictedLoad": self.state.last_prediction,
            "activeModelVersion": self.state.active_model_version,
            "degradedReasons": degraded_reasons,
            "modelLoadTimeMs": self.state.model_load_time_ms,
            "predictionLatencyMs": (
                self.state.predictor.last_prediction_latency_ms
                if self.state.predictor
                else 0.0
            ),
            "scaleBackoffUntil": self.state.next_scale_retry_time or None,
            "lastScaleError": self.state.last_scale_error,
            "circuitBreakerTripped": False,
        }


__all__ = ["ScalerStateMachine"]
