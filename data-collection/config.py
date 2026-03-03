import os

# ── Change ONLY these two lines when switching projects ──
TARGET_APP     = os.getenv("TARGET_APP",      "test-app")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL",  "http://localhost:9090")
NAMESPACE      = os.getenv("NAMESPACE",       "default")
CONTAINER_NAME = os.getenv("CONTAINER_NAME",  "test-app")

# ── All PromQL queries auto-adapt to TARGET_APP ──
QUERIES = {
    # ── Core load signals ──
    "requests_per_second": (
        f'sum(rate(http_requests_total{{'
        f'pod=~"{TARGET_APP}.*"}}[1m]))'
    ),
    "cpu_usage_percent": (
        f'sum(rate(container_cpu_usage_seconds_total{{'
        f'pod=~"{TARGET_APP}.*"}}[1m])) * 100'
    ),
    "memory_usage_bytes": (
        f'sum(container_memory_usage_bytes{{'
        f'pod=~"{TARGET_APP}.*"}})'
    ),
    "latency_p95_ms": (
        f'histogram_quantile(0.95, sum(rate('
        f'http_request_duration_seconds_bucket{{'
        f'pod=~"{TARGET_APP}.*"}}[5m])) by (le)) * 1000'
    ),

    # ── State awareness ──
    "current_replicas": (
        f'kube_deployment_status_replicas_ready{{'
        f'deployment="{TARGET_APP}",'
        f'namespace="{NAMESPACE}"}}'
    ),

    # ── Unique indicators ──
    "active_connections": (
        f'sum(http_connections_active{{'
        f'pod=~"{TARGET_APP}.*"}})'
    ),
    "error_rate": (
        f'sum(rate(http_requests_total{{'
        f'pod=~"{TARGET_APP}.*",'
        f'status=~"4.*|5.*"}}[1m])) / '
        f'sum(rate(http_requests_total{{'
        f'pod=~"{TARGET_APP}.*"}}[1m]))'
    ),

    # ── Momentum signals ──
    "cpu_acceleration": (
        f'sum(rate(container_cpu_usage_seconds_total{{'
        f'pod=~"{TARGET_APP}.*"}}[1m])) * 100'
        f' - sum(rate(container_cpu_usage_seconds_total{{'
        f'pod=~"{TARGET_APP}.*"}}[5m])) * 100'
    ),
    "rps_acceleration": (
        f'sum(rate(http_requests_total{{'
        f'pod=~"{TARGET_APP}.*"}}[1m]))'
        f' - sum(rate(http_requests_total{{'
        f'pod=~"{TARGET_APP}.*"}}[5m]))'
    ),
}

# ── Feature & Target Definitions ──
FEATURE_COLUMNS = [
    # Core load signals
    "requests_per_second",
    "cpu_usage_percent",
    "memory_usage_bytes",
    "latency_p95_ms",

    # One signal each (no duplicates)
    "active_connections",
    "error_rate",

    # Momentum signals
    "cpu_acceleration",
    "rps_acceleration",

    # State awareness
    "current_replicas",

    # Cyclical time
    "hour_sin",
    "hour_cos",
    "dow_sin",
    "dow_cos",
    "is_weekend",
]

TARGET_COLUMNS = [
    "rps_t5",
    "rps_t10",
    "rps_t15",
]
