# operator/features.py — Prometheus → DataFrame
"""Fetch the 9-feature vector from Prometheus and build a pandas DataFrame."""

import requests
import numpy as np
import pandas as pd
from config import PROMETHEUS_URL, TARGET_APP, SCRAPE_WINDOW, LOOKBACK_STEPS


def prom_query(query: str) -> float:
    """Execute an instant PromQL query and return scalar result."""
    r = requests.get(
        f"{PROMETHEUS_URL}/api/v1/query",
        params={"query": query},
        timeout=5,
    )
    result = r.json().get("data", {}).get("result", [])
    if not result:
        return 0.0
    return float(result[0]["value"][1])


def prom_query_range(query: str, start: float, end: float, step: str = "60") -> pd.Series:
    """Execute a range PromQL query and return a time-indexed Series."""
    r = requests.get(
        f"{PROMETHEUS_URL}/api/v1/query_range",
        params={"query": query, "start": start, "end": end, "step": step},
        timeout=10,
    )
    result = r.json().get("data", {}).get("result", [])
    if not result:
        return pd.Series(dtype=float)
    values = result[0]["values"]
    idx = pd.to_datetime([v[0] for v in values], unit="s")
    return pd.Series([float(v[1]) for v in values], index=idx)


def build_feature_vector() -> dict:
    """Fetch current values for all 9 LSTM features.

    Returns dict with feature names as keys.
    """
    app = TARGET_APP
    window = SCRAPE_WINDOW

    rps = prom_query(f'sum(rate(http_requests_total{{pod=~"{app}.*"}}[{window}]))')
    latency = prom_query(
        f'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket'
        f'{{pod=~"{app}.*"}}[5m])) by (le)) * 1000'
    )
    cpu = prom_query(f'sum(rate(container_cpu_usage_seconds_total{{pod=~"{app}.*"}}[{window}]))*100')
    mem = prom_query(f'sum(container_memory_working_set_bytes{{pod=~"{app}.*"}})')
    replicas = prom_query(f'kube_deployment_status_replicas{{deployment="{app}"}}')

    from datetime import datetime
    now = datetime.now()
    hour = now.hour + now.minute / 60.0
    dow = now.weekday()

    return {
        "requests_per_second": rps,
        "latency_p95_ms": latency,
        "cpu_usage_percent": cpu,
        "memory_usage_bytes": mem,
        "hour_sin": np.sin(2 * np.pi * hour / 24),
        "hour_cos": np.cos(2 * np.pi * hour / 24),
        "dow_sin": np.sin(2 * np.pi * dow / 7),
        "dow_cos": np.cos(2 * np.pi * dow / 7),
        "current_replicas": replicas,
    }
