# operator/features.py — fetch live metrics from Prometheus
"""Build the shared LSTM input vector from Prometheus instant queries."""

import logging
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from common.feature_spec import FEATURE_COLUMNS
from common.promql import build_queries, build_fallback_queries
from config import PROMETHEUS_URL, PROM_FAILURE_THRESHOLD

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
