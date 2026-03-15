# operator/features.py — fetch live metrics from Prometheus
"""Build the shared LSTM input vector from Prometheus instant queries."""

import logging
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from common.feature_spec import FEATURE_COLUMNS
from common.promql import build_queries, build_fallback_queries
from config import PROMETHEUS_URL, PROM_FAILURE_THRESHOLD, LOOKBACK_STEPS, TIMER_INTERVAL

logger = logging.getLogger("ppa.features")

# Module-level counter: consecutive Prometheus failures across all queries
_prom_consecutive_failures: int = 0


def prom_query(query: str) -> float | None:
    global _prom_consecutive_failures
    try:
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
            timeout=5,
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
        if _prom_consecutive_failures >= PROM_FAILURE_THRESHOLD:
            logger.error(
                f"Prometheus query failed ({_prom_consecutive_failures} consecutive): {exc}"
            )
        else:
            logger.warning(f"Prometheus query failed: {exc}")
        return None


def build_feature_vector(target_app: str, namespace: str, max_replicas: int, container_name: str | None = None) -> tuple[dict, float]:
    """Fetch current values for all features in the exact training order, returning (features, current_replicas)."""
    queries = build_queries(target_app, namespace, container_name)
    values = {feature_name: prom_query(query) for feature_name, query in queries.items()}

    if values.get("cpu_utilization_pct") is None:
        logger.warning(f"No CPU limits found for {target_app}, falling back to absolute cpu_core_percent")
        fallbacks = build_fallback_queries(target_app, namespace, container_name)
        values["cpu_utilization_pct"] = prom_query(fallbacks["cpu_core_percent"])
        values["cpu_acceleration"] = prom_query(fallbacks["cpu_acceleration"])

    if values.get("memory_utilization_pct") is None:
        logger.warning(f"No memory limits found for {target_app}, falling back to absolute memory_usage_bytes")
        fallbacks = build_fallback_queries(target_app, namespace, container_name)
        values["memory_utilization_pct"] = prom_query(fallbacks["memory_usage_bytes"])

    for k, v in values.items():
        if v is None:
            values[k] = float('nan')

    current_replicas = values.get("current_replicas", float('nan'))
    safe_replicas = current_replicas if not math.isnan(current_replicas) and current_replicas > 0 else 1.0
    
    rps = values.get("requests_per_second", 0.0)
    if math.isnan(rps):
        rps = 0.0

    values["rps_per_replica"] = rps / safe_replicas
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

    return {feature_name: values[feature_name] for feature_name in FEATURE_COLUMNS}, current_replicas


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
