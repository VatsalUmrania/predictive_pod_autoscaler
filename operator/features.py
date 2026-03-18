# operator/features.py — fetch live metrics from Prometheus
"""Build the shared LSTM input vector from Prometheus instant queries."""

import logging
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from common.feature_spec import FEATURE_COLUMNS
from common.promql import build_queries, build_fallback_queries
from config import PROMETHEUS_URL, PROM_FAILURE_THRESHOLD, LOOKBACK_STEPS, TIMER_INTERVAL, FeatureVectorException

logger = logging.getLogger("ppa.features")

# FIX (PR#11): Feature bounds to detect anomalies and prevent extrapolation
# These bounds are based on training data ranges plus tolerance for real-world variance
FEATURE_BOUNDS = {
    'rps_per_replica': (0.01, 100),        # Per-pod RPS from 0.01 to 100
    'cpu_utilization_pct': (0, 150),       # CPU 0-150% (allow some overshoot)
    'memory_utilization_pct': (0, 150),    # Memory 0-150% (allow some overshoot)
    'latency_p95_ms': (1, 10000),          # P95 latency 1-10000 ms
    'active_connections': (0, 100000),     # Connections bounded
    'error_rate': (0, 1),                  # Error rate 0-100%
    'cpu_acceleration': (-100, 100),       # CPU change clamped
    'rps_acceleration': (-100, 100),       # RPS change clamped
    'replicas_normalized': (0, 1),         # Normalized to [0, max_replicas]
    'hour_sin': (-1, 1),                   # Trig bounds
    'hour_cos': (-1, 1),
    'dow_sin': (-1, 1),
    'dow_cos': (-1, 1),
    'is_weekend': (0, 1),                  # Binary
}

# Module-level counter: consecutive Prometheus failures across all queries
_prom_consecutive_failures: int = 0
_prom_last_failure_time: float = 0.0


class PrometheusCircuitBreakerTripped(FeatureVectorException):
    """Raised when Prometheus circuit breaker is active due to repeated failures."""
    pass


def validate_feature_bounds(features: dict) -> tuple[dict, list]:
    """
    FIX (PR#11): Validate features are within expected bounds.
    Returns (validated_features, out_of_bounds_features).
    Clips out-of-bounds values if possible, raises exception if too many features are invalid.
    """
    out_of_bounds = []
    validated = features.copy()

    for feature_name, value in validated.items():
        if feature_name not in FEATURE_BOUNDS:
            continue  # Skip unknown features

        if math.isnan(value) or value is None:
            continue  # Already handled elsewhere

        min_bound, max_bound = FEATURE_BOUNDS[feature_name]

        if value < min_bound or value > max_bound:
            out_of_bounds.append({
                'feature': feature_name,
                'value': value,
                'bounds': (min_bound, max_bound)
            })
            # Log the anomaly
            logger.warning(
                f"Feature {feature_name}={value:.2f} out of bounds [{min_bound}, {max_bound}], clipping"
            )
            # Clip to bounds
            validated[feature_name] = max(min_bound, min(max_bound, value))

    # If >20% of features are out of bounds, raise exception (signal something is very wrong)
    if len(out_of_bounds) > len(FEATURE_BOUNDS) * 0.2:
        raise FeatureVectorException(
            f"Too many features out of bounds ({len(out_of_bounds)}/{len(FEATURE_BOUNDS)}): "
            f"{[f['feature'] for f in out_of_bounds]}"
        )

    return validated, out_of_bounds





def prom_query(query: str) -> float | None:
    """Query Prometheus with exponential backoff on failures. Circuit breaks after PROM_FAILURE_THRESHOLD failures."""
    global _prom_consecutive_failures, _prom_last_failure_time

    # FIX (PR#9): Check circuit breaker status
    if _prom_consecutive_failures >= PROM_FAILURE_THRESHOLD:
        # Circuit breaker active: apply exponential backoff
        backoff_time = min(300, 2 ** min(_prom_consecutive_failures - PROM_FAILURE_THRESHOLD, 10))
        elapsed = time.time() - _prom_last_failure_time
        if elapsed < backoff_time:
            # Still in backoff, raise exception to block feature extraction
            raise PrometheusCircuitBreakerTripped(
                f"Prometheus circuit breaker active for {elapsed:.0f}s (backoff: {backoff_time}s)"
            )

    try:
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
            timeout=2,  # Short timeout to fail fast
        )
        resp.raise_for_status()
        payload = resp.json()
        result = payload.get("data", {}).get("result", [])
        if not result:
            return None
        _prom_consecutive_failures = 0  # reset on success
        return float(result[0]["value"][1])
    except Exception as exc:
        _prom_consecutive_failures += 1
        _prom_last_failure_time = time.time()
        if _prom_consecutive_failures >= PROM_FAILURE_THRESHOLD:
            logger.critical(
                f"Prometheus circuit breaker TRIPPED ({_prom_consecutive_failures}/{PROM_FAILURE_THRESHOLD} failures): {exc}"
            )
            raise PrometheusCircuitBreakerTripped(str(exc))
        else:
            logger.warning(f"Prometheus query failed ({_prom_consecutive_failures}/{PROM_FAILURE_THRESHOLD}): {exc}")
        return None


def build_feature_vector(target_app: str, namespace: str, reference_replicas: int, max_replicas: int, container_name: str | None = None) -> tuple[dict, float]:
    """Fetch current values for all features in the exact training order, returning (features, current_replicas)."""
    queries = build_queries(target_app, namespace, container_name)
    values = {feature_name: prom_query(query) for feature_name, query in queries.items()}

    if values.get("cpu_utilization_pct") is None:
        # FIX (PR#6): Don't fall back to absolute CPU cores (mixing units)
        # Raise exception instead to force user to set resource requests
        raise FeatureVectorException(f"CPU utilization unavailable for {target_app}, resource requests not set on target deployment")

    if values.get("memory_utilization_pct") is None:
        raise FeatureVectorException(f"Memory utilization unavailable for {target_app}, resource requests not set on target deployment")

    # FIX (PR#4): Don't silently convert None → NaN
    # Instead, check for critical missing features and raise exception
    critical_features = ["cpu_utilization_pct", "memory_utilization_pct", "current_replicas", "requests_per_second"]
    missing_features = [f for f in critical_features if values.get(f) is None]

    if missing_features:
        # Raise exception instead of silently proceeding with NaN
        raise FeatureVectorException(f"Missing critical features: {missing_features}")

    # Non-critical features can be NaN (will be handled later)
    for k, v in values.items():
        if v is None and k not in critical_features:
            values[k] = float('nan')

    current_replicas = values.get("current_replicas", float('nan'))
    safe_replicas = current_replicas if not math.isnan(current_replicas) and current_replicas > 0 else 1.0

    rps = values.get("requests_per_second", 0.0)
    if math.isnan(rps):
        rps = 0.0

    # FIX: Use reference_replicas (stable) instead of current_replicas (volatile)
    # This ensures the feature has consistent meaning across scale events
    stable_ref = max(reference_replicas, 1)  # Clamp to 1 to avoid division by zero
    values["rps_per_replica"] = rps / stable_ref
    values["replicas_normalized"] = current_replicas / float(max_replicas)

    now = datetime.now(timezone.utc)
    hour = now.hour + now.minute / 60.0 + now.second / 3600.0
    dow = now.weekday()

    values.update(
        {
            "hour_sin": np.sin(2 * np.pi * hour / 24),
            "hour_cos": np.cos(2 * np.pi * hour / 24),
            "dow_sin": np.sin(2 * np.pi * dow / 7),
            "dow_cos": np.cos(2 * np.pi * dow / 7),
            "is_weekend": float(dow >= 5),
        }
    )

    # FIX (PR#11): Validate all features are within expected bounds
    final_features = {feature_name: values[feature_name] for feature_name in FEATURE_COLUMNS}
    final_features, oob = validate_feature_bounds(final_features)

    if oob:
        logger.info(f"Clipped {len(oob)} out-of-bounds features")

    return final_features, current_replicas


def prom_range_query(query: str, step_seconds: int = 30, hours: int = 1) -> dict[float, float]:
    """
    Fetch a time-range of metric values from Prometheus.
    Returns dict mapping Unix timestamp -> float value.
    """
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query_range",
            params={
                "query": query,
                "start": int(start.timestamp()),
                "end": int(end.timestamp()),
                "step": f"{step_seconds}s",
            },
            timeout=60,
        )
        resp.raise_for_status()
        payload = resp.json()
        result = payload.get("data", {}).get("result", [])
        if not result:
            return {}
        
        # Extract timeseries: [(timestamp, value), ...]
        values = result[0].get("values", [])
        return {float(ts): float(val) for ts, val in values if val not in ("NaN", None)}
    except Exception as exc:
        logger.error(f"Range query failed: {exc}")
        return {}


def build_historical_features(
    target_app: str,
    namespace: str,
    max_replicas: int,
    container_name: str | None = None,
    lookback_steps: int = LOOKBACK_STEPS,
    step_seconds: int = TIMER_INTERVAL,
) -> list[dict]:
    """
    Fetch the last lookback_steps worth of data from Prometheus and reconstruct feature vectors.
    Returns list of feature dicts in chronological order (oldest first).
    """
    queries = build_queries(target_app, namespace, container_name)
    fallbacks = build_fallback_queries(target_app, namespace, container_name)
    
    # Fetch all metrics over the lookback window
    total_seconds = lookback_steps * step_seconds
    hours = total_seconds / 3600.0
    
    logger.info(f"Fetching {lookback_steps} steps ({hours:.1f}h) of historical data with step={step_seconds}s...")
    
    metric_timeseries = {}
    for feature_name, query in queries.items():
        if feature_name in ["cpu_acceleration", "rps_acceleration"]:
            continue  # Skip acceleration (derived later)
        
        ts_data = prom_range_query(query, step_seconds=step_seconds, hours=max(hours, 1))
        if not ts_data and feature_name == "cpu_utilization_pct":
            logger.warning(f"No CPU limits, trying fallback cpu_core_percent")
            ts_data = prom_range_query(fallbacks["cpu_core_percent"], step_seconds=step_seconds, hours=max(hours, 1))
        if not ts_data and feature_name == "memory_utilization_pct":
            logger.warning(f"No memory limits, trying fallback memory_usage_bytes")
            ts_data = prom_range_query(fallbacks["memory_usage_bytes"], step_seconds=step_seconds, hours=max(hours, 1))
        
        metric_timeseries[feature_name] = ts_data
    
    # Align all timeseries to the same set of timestamps (use rps_per_second as anchor, or any queried metric)
    all_timestamps = set()
    for ts_data in metric_timeseries.values():
        all_timestamps.update(ts_data.keys())
    
    if not all_timestamps:
        logger.warning("No historical data fetched; returning empty list")
        return []
    
    sorted_timestamps = sorted(all_timestamps)
    
    # Reconstruct feature vectors at each timestamp
    feature_rows = []
    for ts in sorted_timestamps[-lookback_steps:]:  # Keep only last lookback_steps
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        
        # Get values for this timestamp (fill NaN with 0)
        values = {}
        for feature_name in queries.keys():
            if feature_name in ["cpu_acceleration", "rps_acceleration"]:
                continue
            values[feature_name] = metric_timeseries.get(feature_name, {}).get(ts, float('nan'))
        
        # Derived features from raw metrics
        current_replicas = values.get("current_replicas", float('nan'))
        safe_replicas = current_replicas if not math.isnan(current_replicas) and current_replicas > 0 else 1.0
        
        rps = values.get("requests_per_second", 0.0)
        if math.isnan(rps):
            rps = 0.0
        
        values["rps_per_replica"] = rps / safe_replicas
        values["replicas_normalized"] = current_replicas / float(max_replicas)
        
        # Temporal features based on historical timestamp (not current time)
        hour = dt.hour + dt.minute / 60.0 + dt.second / 3600.0
        dow = dt.weekday()
        
        values.update({
            "hour_sin": np.sin(2 * np.pi * hour / 24),
            "hour_cos": np.cos(2 * np.pi * hour / 24),
            "dow_sin": np.sin(2 * np.pi * dow / 7),
            "dow_cos": np.cos(2 * np.pi * dow / 7),
            "is_weekend": float(dow >= 5),
        })
        
        # Ensure we have values for all columns (fill NaN with 0)
        for col in FEATURE_COLUMNS:
            if col not in values or math.isnan(values[col]):
                values[col] = 0.0
        
        feature_rows.append(values)
    
    logger.info(f"Reconstructed {len(feature_rows)} historical feature vectors")
    return feature_rows
