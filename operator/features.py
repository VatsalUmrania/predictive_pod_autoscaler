# operator/features.py — fetch live metrics from Prometheus
"""Build the 14-feature LSTM input vector from Prometheus instant queries."""

import numpy as np
import requests
import logging

from config import PROMETHEUS_URL, SCRAPE_WINDOW

logger = logging.getLogger("ppa.features")


def prom_query(query: str) -> float:
    try:
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
            timeout=5,
        )
        result = resp.json()["data"]["result"]
        return float(result[0]["value"][1]) if result else 0.0
    except Exception as e:
        logger.warning(f"Prometheus query failed: {e}")
        return 0.0


def build_feature_vector(target_app: str, namespace: str) -> dict:
    """Fetch current values for all 14 LSTM features.

    Args:
        target_app: Deployment name (e.g. "test-app").
        namespace:  K8s namespace (e.g. "default").

    Returns dict with feature names as keys in EXACT order matching training CSV.
    """
    app = target_app
    ns = namespace
    window = SCRAPE_WINDOW

    # Core load signals (namespace-scoped)
    rps = prom_query(f'sum(rate(http_requests_total{{pod=~"{app}.*",namespace="{ns}"}}[{window}]))')
    cpu = prom_query(f'sum(rate(container_cpu_usage_seconds_total{{pod=~"{app}.*",namespace="{ns}"}}[{window}])) * 100')
    mem = prom_query(f'sum(container_memory_usage_bytes{{pod=~"{app}.*",namespace="{ns}"}})')
    latency = prom_query(
        f'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket'
        f'{{pod=~"{app}.*",namespace="{ns}"}}[5m])) by (le)) * 1000'
    )

    # State awareness
    replicas = prom_query(f'kube_deployment_status_replicas_ready{{deployment="{app}",namespace="{ns}"}}')

    # Unique indicators
    connections = prom_query(f'sum(http_connections_active{{pod=~"{app}.*",namespace="{ns}"}})')

    # Error rate (safe division)
    err_rate = 0.0
    errors = prom_query(f'sum(rate(http_requests_total{{pod=~"{app}.*",namespace="{ns}",status=~"4.*|5.*"}}[{window}]))')
    if rps > 0:
        err_rate = errors / rps

    # Momentum signals
    cpu_5m = prom_query(f'sum(rate(container_cpu_usage_seconds_total{{pod=~"{app}.*",namespace="{ns}"}}[5m])) * 100')
    cpu_accel = cpu - cpu_5m

    rps_5m = prom_query(f'sum(rate(http_requests_total{{pod=~"{app}.*",namespace="{ns}"}}[5m]))')
    rps_accel = rps - rps_5m

    # Cyclical time
    from datetime import datetime
    now = datetime.now()
    hour = now.hour + now.minute / 60.0
    dow = now.weekday()

    return {
        "requests_per_second": rps,
        "cpu_usage_percent": cpu,
        "memory_usage_bytes": mem,
        "latency_p95_ms": latency,
        "active_connections": connections,
        "error_rate": err_rate,
        "cpu_acceleration": cpu_accel,
        "rps_acceleration": rps_accel,
        "current_replicas": replicas,
        "hour_sin": np.sin(2 * np.pi * hour / 24),
        "hour_cos": np.cos(2 * np.pi * hour / 24),
        "dow_sin": np.sin(2 * np.pi * dow / 7),
        "dow_cos": np.cos(2 * np.pi * dow / 7),
        "is_weekend": float(dow >= 5),
    }
