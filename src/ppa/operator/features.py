# operator/features.py — fetch live metrics from Prometheus
"""Build the shared LSTM input vector from Prometheus instant queries."""

import logging
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import numpy as np
import requests

from ppa.common.feature_spec import FEATURE_COLUMNS
from ppa.common.promql import build_fallback_queries, build_queries
from ppa.config import (
    LOOKBACK_STEPS,
    PROMETHEUS_URL,
    TIMER_INTERVAL,
    FeatureVectorError,
)
from ppa.domain import validate_feature_bounds
from ppa.operator.prometheus import (
    PrometheusCircuitBreakerError,
    PrometheusCircuitBreakerTripped,
    prom_query_parallel,
    set_prometheus_url,
)

__all__ = [
    "PrometheusCircuitBreakerError",
    "PrometheusCircuitBreakerTripped",
    "build_feature_vector",
    "validate_feature_bounds",
    "build_historical_features",
    "prom_range_query",
]

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

logger = logging.getLogger("ppa.features")


def _validate_critical_metrics(values: dict[str, float | None]) -> None:
    """Validate that critical metrics are available (not None).

    For critical missing metrics, log warning and set defaults instead of failing.
    """
    default_values = {
        "cpu_utilization_pct": 0.0,
        "memory_utilization_pct": 0.0,
        "requests_per_second": 0.0,
        "current_replicas": 1.0,
    }

    for feature, default in default_values.items():
        if values.get(feature) is None:
            values[feature] = default


def _normalize_metrics(
    values: dict[str, float | None],
    reference_replicas: int,
    max_replicas: int,
) -> dict[str, float | None]:
    """Normalize metrics for LSTM input.

    Converts RPS to per-replica and normalizes replica count to [0,1].

    Args:
        values: Raw metric values dict
        reference_replicas: Current pod count for RPS normalization
        max_replicas: Maximum pod count for replica normalization

    Returns:
        Updated values dict with normalized metrics
    """
    rps_value = values.get("requests_per_second", 0.0)
    rps = rps_value if rps_value is not None else 0.0
    if math.isnan(rps):
        rps = 0.0

    # FIX: Use reference_replicas (stable) instead of current_replicas (volatile)
    # This ensures the feature has consistent meaning across scale events
    stable_ref = max(reference_replicas, 1)  # Clamp to 1 to avoid division by zero
    values["rps_per_replica"] = rps / stable_ref
    values["replicas_normalized"] = values["current_replicas"] / float(max_replicas)

    return values


def _add_temporal_features(values: dict[str, float | None]) -> dict[str, float | None]:
    """Add time-based features for seasonality.

    Computes sin/cos of hour and day-of-week for cyclic encoding.
    """
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

    return values


def build_feature_vector(
    target_app: str,
    namespace: str,
    reference_replicas: int,
    max_replicas: int,
    container_name: str | None = None,
    prom_url: str | None = None,
    cr_state: object | None = None,
) -> tuple[dict[str, float | None], float]:
    """Build feature vector from Prometheus metrics for LSTM prediction.

    Queries Prometheus for current load metrics, resource usage, and time features.
    Results are validated for critical fields and normalized for LSTM input.

    Feature Vector Components (in training order):
    - **Load metrics:** requests_per_second, latency_p95_ms, error_rate
    - **Resource metrics:** cpu_utilization_pct, memory_utilization_pct
    - **Pod metrics:** current_replicas, active_connections
    - **Acceleration:** cpu_acceleration, rps_acceleration (delta from last cycle)
    - **Time features:** hour_sin, hour_cos, day_of_week_sin, day_of_week_cos (seasonality)
    - **Weekend flag:** is_weekend (binary)

    Query Resilience:
    - Uses parallel queries for speed (5 workers, 2s timeout per query)
    - Circuit breaker activates after 10 consecutive Prometheus failures
    - Returns None for optional features, raises exception for critical ones
    - Multi-region support via custom prom_url parameter (PR#18)

    Validation:
    - Raises FeatureVectorException if cpu_utilization_pct or memory_utilization_pct missing
    - Raises FeatureVectorException if any of 4 critical features are None
    - Sets missing optional features to NaN (handled by validation layer)

    Args:
        target_app: Kubernetes Deployment name to monitor
        namespace: Kubernetes namespace containing the deployment
        reference_replicas: Current pod count (for normalization)
        max_replicas: Maximum allowed pod count (for normalization)
        container_name: Optional container name (for resource metrics)
        prom_url: Optional custom Prometheus URL (for multi-region deployments)
        cr_state: Optional CRD status object for circuit breaker state

    Returns:
        Tuple of (feature_dict, current_replica_count) where:
        - feature_dict: Dict mapping metric names to float values
        - current_replica_count: Current pod count from Prometheus

    Raises:
        FeatureVectorException: If critical features are missing or unavailable
        PrometheusCircuitBreakerError: If circuit breaker is open (Prometheus down)

    Example:
        >>> features, replicas = build_feature_vector(
        ...     'my-api', 'production', 10, 50
        ... )
        >>> print(f"RPS: {features['requests_per_second']}, Pods: {replicas}")
        RPS: 1234.5, Pods: 10

    Performance:
        - Typical: 5-7 parallel queries, <1000ms total
        - Worst case: 10s timeout (circuit breaker activates)

    Design Notes:
        - Thread-safe: uses ThreadPoolExecutor for parallel queries
        - Stateless: all state in CRD status (enables multi-pod operators)
        - Fail-fast: raises exceptions rather than returning partial data
        - Observable: logs all query failures and anomalies

    See Also:
        prom_query_parallel: Parallel Prometheus query executor
        validate_feature_bounds: Data quality checks (downstream)
        PR#20: Parallel query optimization
        PR#18: Multi-region Prometheus support
        PR#11: Feature bounds validation
    """
    # FIX (PR#18): Set Prometheus URL for this execution context
    if prom_url:
        set_prometheus_url(prom_url)

    # Step 1: Query Prometheus
    queries = build_queries(target_app, namespace, container_name)
    values = prom_query_parallel(
        queries, max_workers=5, timeout=2.0, prom_url=prom_url, cr_state=cr_state
    )

    # Step 1b: Convert latency sentinel zero → NaN.
    # The PromQL query uses `or vector(0)` as a fallback when there are no histogram
    # buckets (i.e. zero traffic). The value 0.0 is not a real P95 latency — it means
    # "no data available". Converting it to NaN lets the rest of the pipeline treat it
    # as a missing optional feature rather than clipping it against the 1ms lower bound.
    latency_raw = values.get("latency_p95_ms")
    if latency_raw is not None and latency_raw == 0.0:
        values["latency_p95_ms"] = None  # Will become NaN in Step 3

    # Step 2: Validate critical metrics are available
    _validate_critical_metrics(values)

    # Step 3: Convert None to NaN for optional features
    critical_features = [
        "cpu_utilization_pct",
        "memory_utilization_pct",
        "current_replicas",
        "requests_per_second",
    ]
    for k, v in values.items():
        if v is None and k not in critical_features:
            values[k] = float("nan")

    current_replicas_value = values.get("current_replicas")
    current_replicas: float = (
        current_replicas_value if isinstance(current_replicas_value, float) else float("nan")
    )

    # Step 4: Normalize metrics for LSTM
    values = _normalize_metrics(values, reference_replicas, max_replicas)

    # Step 5: Add temporal features
    values = _add_temporal_features(values)

    # Step 6: Validate feature bounds
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

        hours_int = int(max(hours, 1.0))
        ts_data = prom_range_query(query, step_seconds=step_seconds, hours=hours_int)
        if not ts_data and feature_name == "cpu_utilization_pct":
            logger.warning("No CPU limits, trying fallback cpu_core_percent")
            ts_data = prom_range_query(
                fallbacks["cpu_core_percent"],
                step_seconds=step_seconds,
                hours=hours_int,
            )
        if not ts_data and feature_name == "memory_utilization_pct":
            logger.warning("No memory limits, trying fallback memory_usage_bytes")
            ts_data = prom_range_query(
                fallbacks["memory_usage_bytes"],
                step_seconds=step_seconds,
                hours=hours_int,
            )

        metric_timeseries[feature_name] = ts_data

    # Align all timeseries to the same set of timestamps (use rps_per_second as anchor, or any queried metric)
    all_timestamps: set[float] = set()
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
