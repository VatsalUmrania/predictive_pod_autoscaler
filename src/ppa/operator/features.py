# operator/features.py — fetch live metrics from Prometheus
"""Build the shared LSTM input vector from Prometheus instant queries."""

import logging
import math
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import numpy as np
import requests  # type: ignore[import-untyped]

from ppa.common.feature_spec import FEATURE_COLUMNS
from ppa.common.promql import build_fallback_queries, build_queries
from ppa.config import (
    LOOKBACK_STEPS,
    PROM_FAILURE_THRESHOLD,
    PROMETHEUS_URL,
    TIMER_INTERVAL,
    FeatureVectorError,
)

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

logger = logging.getLogger("ppa.features")

# FIX (PR#11): Feature bounds to detect anomalies and prevent extrapolation
# These bounds are based on training data ranges plus tolerance for real-world variance
FEATURE_BOUNDS = {
    "rps_per_replica": (0.01, 100),  # Per-pod RPS from 0.01 to 100
    "cpu_utilization_pct": (0, 150),  # CPU 0-150% (allow some overshoot)
    "memory_utilization_pct": (0, 150),  # Memory 0-150% (allow some overshoot)
    "latency_p95_ms": (1, 10000),  # P95 latency 1-10000 ms
    "active_connections": (0, 100000),  # Connections bounded
    "error_rate": (0, 1),  # Error rate 0-100%
    "cpu_acceleration": (-100, 100),  # CPU change clamped
    "rps_acceleration": (-100, 100),  # RPS change clamped
    "replicas_normalized": (0, 1),  # Normalized to [0, max_replicas]
    "hour_sin": (-1, 1),  # Trig bounds
    "hour_cos": (-1, 1),
    "dow_sin": (-1, 1),
    "dow_cos": (-1, 1),
    "is_weekend": (0, 1),  # Binary
}

# Module-level counter: consecutive Prometheus failures across all queries
# NOTE: This is deprecated. Use CRState.prom_failures and CRState.prom_last_failure_time instead.
# Keeping for backward compatibility with standalone tools.
_prom_consecutive_failures: int = 0
_prom_last_failure_time: float = 0.0


class PrometheusCircuitBreakerError(FeatureVectorError):
    """Raised when Prometheus circuit breaker is active due to repeated failures."""

    pass


# Backward compatibility alias
PrometheusCircuitBreakerTripped = PrometheusCircuitBreakerError


def _get_circuit_breaker(cr_state: object | None) -> tuple[int, float]:
    """Get circuit breaker state from CR state or module-level fallback."""
    if cr_state is not None and hasattr(cr_state, 'prom_failures'):
        return cr_state.prom_failures, cr_state.prom_last_failure_time  # type: ignore[attr-defined]
    return _prom_consecutive_failures, _prom_last_failure_time


def _set_circuit_breaker(cr_state: object | None, failures: int, last_time: float) -> None:
    """Set circuit breaker state in CR state or module-level fallback."""
    if cr_state is not None and hasattr(cr_state, 'prom_failures'):
        cr_state.prom_failures = failures  # type: ignore[attr-defined]
        cr_state.prom_last_failure_time = last_time  # type: ignore[attr-defined]
    else:
        global _prom_consecutive_failures, _prom_last_failure_time
        _prom_consecutive_failures = failures
        _prom_last_failure_time = last_time


def validate_feature_bounds(features: dict) -> tuple[dict[str, float | None], list]:
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
            out_of_bounds.append(
                {
                    "feature": feature_name,
                    "value": value,
                    "bounds": (min_bound, max_bound),
                }
            )
            # Log the anomaly
            logger.warning(
                f"Feature {feature_name}={value:.2f} out of bounds [{min_bound}, {max_bound}], clipping"
            )
            # Clip to bounds
            validated[feature_name] = max(min_bound, min(max_bound, value))

    # If >20% of features are out of bounds, raise exception (signal something is very wrong)
    if len(out_of_bounds) > len(FEATURE_BOUNDS) * 0.2:
        raise FeatureVectorError(
            f"Too many features out of bounds ({len(out_of_bounds)}/{len(FEATURE_BOUNDS)}): "
            f"{[f['feature'] for f in out_of_bounds]}"
        )

    return validated, out_of_bounds


# FIX (PR#18): Per-query Prometheus URL storage for multi-region support
# Uses threading.local() to ensure each thread has its own URL without cross-thread contamination
_thread_local_storage = threading.local()


def set_prometheus_url(url: str) -> None:
    """Set the Prometheus URL for the current thread's execution context (thread-safe via threading.local).
    
    This enables multi-region support where different threads (e.g., in prom_query_parallel)
    can query different Prometheus instances without interfering with each other.
    """
    _thread_local_storage.prom_url = url


def get_current_prometheus_url() -> str:
    """Get the Prometheus URL for the current thread.
    
    Returns the thread-local URL if set, otherwise defaults to PROMETHEUS_URL.
    Thread-local isolation prevents one thread's URL from affecting another.
    """
    return getattr(_thread_local_storage, 'prom_url', PROMETHEUS_URL)


def prom_query(query: str, prom_url: str | None = None, cr_state: object | None = None) -> float | None:
    """Query Prometheus with exponential backoff on failures. Circuit breaks after PROM_FAILURE_THRESHOLD failures.
    
    Args:
        query: PromQL query string
        prom_url: Optional custom Prometheus URL (PR#18: multi-region support)
        cr_state: Optional CRState for per-CR circuit breaker (PR#1 fix)
    """
    # FIX (PR#1): Get circuit breaker state (per-CR or fallback to module-level)
    prom_failures, prom_last_failure_time = _get_circuit_breaker(cr_state)

    # FIX (PR#18): Use provided URL or fall back to current context URL
    url = prom_url or get_current_prometheus_url()

    # FIX (PR#9): Check circuit breaker status
    if prom_failures >= PROM_FAILURE_THRESHOLD:
        # Circuit breaker active: apply exponential backoff
        backoff_time = min(300, 2 ** min(prom_failures - PROM_FAILURE_THRESHOLD, 10))
        elapsed = time.time() - prom_last_failure_time
        if elapsed < backoff_time:
            # Still in backoff, raise exception to block feature extraction
            raise PrometheusCircuitBreakerTripped(
                f"Prometheus circuit breaker active for {elapsed:.0f}s (backoff: {backoff_time}s)"
            )

    try:
        resp = requests.get(
            f"{url}/api/v1/query",
            params={"query": query},
            timeout=2,  # Short timeout to fail fast
        )
        resp.raise_for_status()
        payload = resp.json()
        result = payload.get("data", {}).get("result", [])
        if not result:
            return None
        _set_circuit_breaker(cr_state, 0, 0.0)  # Reset on success
        return float(result[0]["value"][1])
    except Exception as exc:
        prom_failures += 1
        prom_last_failure_time = time.time()
        _set_circuit_breaker(cr_state, prom_failures, prom_last_failure_time)
        
        if prom_failures >= PROM_FAILURE_THRESHOLD:
            logger.critical(
                f"Prometheus circuit breaker TRIPPED ({prom_failures}/{PROM_FAILURE_THRESHOLD} failures): {exc}"
            )
            raise PrometheusCircuitBreakerError(str(exc)) from exc
        else:
            logger.warning(
                f"Prometheus query failed ({prom_failures}/{PROM_FAILURE_THRESHOLD}): {exc}"
            )
        return None


def prom_query_parallel(
    queries: dict[str, str],
    max_workers: int = 5,
    timeout: float = 2.0,
    prom_url: str | None = None,
    cr_state: object | None = None,
) -> dict[str, float | None]:
    """
    FIX (PR#20): Execute Prometheus queries in parallel using ThreadPoolExecutor.

    Args:
        queries: Dict mapping feature_name -> promql query string
        max_workers: Number of parallel threads (default 5)
        timeout: Per-query timeout in seconds
        prom_url: Optional custom Prometheus URL (PR#18: multi-region support)
        cr_state: Optional CRState for per-CR circuit breaker (PR#1 fix)

    Returns:
        Dict mapping feature_name -> result (float or None if failed)
    """
    results: dict[str, float | None] = {}

    def _query_single(feature_query_pair: tuple[str, str]) -> tuple[str, float | None]:
        feature_name, query = feature_query_pair
        try:
            result = prom_query(query, prom_url=prom_url, cr_state=cr_state)  # PR#18: pass custom URL; PR#1: pass CR state
            return feature_name, result
        except PrometheusCircuitBreakerError:
            raise  # Re-raise circuit breaker to stop all queries
        except Exception as exc:
            logger.debug(f"Query failed for {feature_name}: {exc}")
            return feature_name, None

    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all queries
            future_to_feature = {
                executor.submit(_query_single, (name, query)): name
                for name, query in queries.items()
            }

            # Collect results with timeout
            for future in future_to_feature:
                feature_name = future_to_feature[future]
                try:
                    _, result = future.result(timeout=timeout)
                    results[feature_name] = result
                except FutureTimeoutError:
                    logger.warning(f"Query timeout for {feature_name}")
                    results[feature_name] = None
                except PrometheusCircuitBreakerError:
                    raise  # Propagate circuit breaker
                except Exception as exc:
                    logger.debug(f"Query exception for {feature_name}: {exc}")
                    results[feature_name] = None

    except PrometheusCircuitBreakerError:
        # Circuit breaker active - all queries should fail
        logger.error("Circuit breaker active, aborting parallel queries")
        raise

    return results


def build_feature_vector(
    target_app: str,
    namespace: str,
    reference_replicas: int,
    max_replicas: int,
    container_name: str | None = None,
    prom_url: str | None = None,
    cr_state: object | None = None,
) -> tuple[dict[str, float | None], float]:
    """Fetch current values for all features in the exact training order, returning (features, current_replicas)."""
    # FIX (PR#18): Set Prometheus URL for this execution context
    if prom_url:
        set_prometheus_url(prom_url)

    queries = build_queries(target_app, namespace, container_name)
    # FIX (PR#20): Use parallel queries instead of sequential
    # FIX (PR#18): Pass custom Prometheus URL for multi-region support
    # FIX (PR#1): Pass CR state for per-CR circuit breaker isolation
    values = prom_query_parallel(queries, max_workers=5, timeout=2.0, prom_url=prom_url, cr_state=cr_state)

    if values.get("cpu_utilization_pct") is None:
        # FIX (PR#6): Don't fall back to absolute CPU cores (mixing units)
        # Raise exception instead to force user to set resource requests
        raise FeatureVectorError(
            f"CPU utilization unavailable for {target_app}, resource requests not set on target deployment"
        )

    if values.get("memory_utilization_pct") is None:
        raise FeatureVectorError(
            f"Memory utilization unavailable for {target_app}, resource requests not set on target deployment"
        )

    # FIX (PR#4): Don't silently convert None → NaN
    # Instead, check for critical missing features and raise exception
    critical_features = [
        "cpu_utilization_pct",
        "memory_utilization_pct",
        "current_replicas",
        "requests_per_second",
    ]
    missing_features = [f for f in critical_features if values.get(f) is None]

    if missing_features:
        # Raise exception instead of silently proceeding with NaN
        raise FeatureVectorError(f"Missing critical features: {missing_features}")

    # Non-critical features can be NaN (will be handled later)
    for k, v in values.items():
        if v is None and k not in critical_features:
            values[k] = float("nan")

    current_replicas = values.get("current_replicas", float("nan"))  # type: ignore[assignment]

    rps = values.get("requests_per_second", 0.0)  # type: ignore[assignment]
    if math.isnan(rps):  # type: ignore[arg-type]
        rps = 0.0

    # FIX: Use reference_replicas (stable) instead of current_replicas (volatile)
    # This ensures the feature has consistent meaning across scale events
    stable_ref = max(reference_replicas, 1)  # Clamp to 1 to avoid division by zero
    values["rps_per_replica"] = rps / stable_ref  # type: ignore[operator]
    values["replicas_normalized"] = current_replicas / float(max_replicas)  # type: ignore[operator]

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

    return final_features, float(current_replicas)  # type: ignore[arg-type]


def prom_range_query(query: str, step_seconds: int = 30, hours: int = 1) -> dict[float, float]:
    """
    Fetch a time-range of metric values from Prometheus.
    Returns dict mapping Unix timestamp -> float value.
    """
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        params: dict[str, str | int] = {
            "query": query,
            "start": int(start.timestamp()),
            "end": int(end.timestamp()),
            "step": f"{step_seconds}s",
        }
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query_range",
            params=cast(dict[str, str | int | float], params),
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

    logger.info(
        f"Fetching {lookback_steps} steps ({hours:.1f}h) of historical data with step={step_seconds}s..."
    )

    metric_timeseries = {}
    for feature_name, query in queries.items():
        if feature_name in ["cpu_acceleration", "rps_acceleration"]:
            continue  # Skip acceleration (derived later)

        ts_data = prom_range_query(query, step_seconds=step_seconds, hours=int(max(hours, 1)))  # type: ignore[arg-type]
        if not ts_data and feature_name == "cpu_utilization_pct":
            logger.warning("No CPU limits, trying fallback cpu_core_percent")
            ts_data = prom_range_query(
                fallbacks["cpu_core_percent"],
                step_seconds=step_seconds,
                hours=int(max(hours, 1)),  # type: ignore[arg-type]
            )
        if not ts_data and feature_name == "memory_utilization_pct":
            logger.warning("No memory limits, trying fallback memory_usage_bytes")
            ts_data = prom_range_query(
                fallbacks["memory_usage_bytes"],
                step_seconds=step_seconds,
                hours=int(max(hours, 1)),  # type: ignore[arg-type]
            )

        metric_timeseries[feature_name] = ts_data

    # Align all timeseries to the same set of timestamps (use rps_per_second as anchor, or any queried metric)
    all_timestamps: set[float] = set()  # type: ignore[var-annotated]
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
            values[feature_name] = metric_timeseries.get(feature_name, {}).get(ts, float("nan"))

        # Derived features from raw metrics
        current_replicas = values.get("current_replicas", float("nan"))
        safe_replicas = (
            current_replicas if not math.isnan(current_replicas) and current_replicas > 0 else 1.0
        )

        rps = values.get("requests_per_second", 0.0)
        if math.isnan(rps):
            rps = 0.0

        values["rps_per_replica"] = rps / safe_replicas
        values["replicas_normalized"] = current_replicas / float(max_replicas)

        # Temporal features based on historical timestamp (not current time)
        hour = dt.hour + dt.minute / 60.0 + dt.second / 3600.0
        dow = dt.weekday()

        values.update(
            {
                "hour_sin": np.sin(2 * np.pi * hour / 24),
                "hour_cos": np.cos(2 * np.pi * hour / 24),
                "dow_sin": np.sin(2 * np.pi * dow / 7),
                "dow_cos": np.cos(2 * np.pi * dow / 7),
                "is_weekend": float(dow >= 5),
            }
        )

        # Ensure we have values for all columns (fill NaN with 0)
        for col in FEATURE_COLUMNS:
            if col not in values or math.isnan(values[col]):
                values[col] = 0.0

        feature_rows.append(values)

    logger.info(f"Reconstructed {len(feature_rows)} historical feature vectors")
    return feature_rows
