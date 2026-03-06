# operator/features.py — fetch live metrics from Prometheus
"""Build the shared LSTM input vector from Prometheus instant queries."""

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from common.feature_spec import FEATURE_COLUMNS
from common.promql import build_queries
from config import PROMETHEUS_URL

logger = logging.getLogger("ppa.features")


def prom_query(query: str) -> float:
    try:
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
            timeout=5,
        )
        resp.raise_for_status()
        payload = resp.json()
        result = payload.get("data", {}).get("result", [])
        return float(result[0]["value"][1]) if result else 0.0
    except Exception as exc:
        logger.warning(f"Prometheus query failed: {exc}")
        return 0.0


def build_feature_vector(target_app: str, namespace: str, container_name: str | None = None) -> dict:
    """Fetch current values for all features in the exact training order."""
    queries = build_queries(target_app, namespace, container_name)
    values = {feature_name: prom_query(query) for feature_name, query in queries.items()}

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

    return {feature_name: values[feature_name] for feature_name in FEATURE_COLUMNS}
