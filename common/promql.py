"""PromQL query builders shared by the data collector and operator."""

RATE_WINDOW = "1m"
LATENCY_WINDOW = "5m"
BASELINE_WINDOW = "5m"


def _matchers(target_app: str, namespace: str, container_name: str | None = None) -> str:
    parts = [
        f'pod=~"{target_app}.*"',
        f'namespace="{namespace}"',
    ]
    if container_name:
        parts.append(f'container="{container_name}"')
    return ",".join(parts)


def build_queries(target_app: str, namespace: str, container_name: str | None = None) -> dict[str, str]:
    """Return PromQL for all queried features in training/inference order."""
    app_matchers = _matchers(target_app, namespace)
    # kubelet/cAdvisor label sets differ across environments; pod+namespace is the
    # most portable selector for this project and still resolves to the test app only.
    resource_matchers = _matchers(target_app, namespace)

    return {
        "requests_per_second": (
            f'sum(rate(http_requests_total{{{app_matchers}}}[{RATE_WINDOW}]))'
        ),
        "cpu_usage_percent": (
            f'avg(rate(container_cpu_usage_seconds_total{{{resource_matchers}}}[{RATE_WINDOW}])) * 100'
        ),
        "memory_usage_bytes": (
            f'avg(container_memory_working_set_bytes{{{resource_matchers}}})'
        ),
        "latency_p95_ms": (
            f'histogram_quantile(0.95, sum(rate('
            f'http_request_duration_seconds_bucket{{{app_matchers}}}[{LATENCY_WINDOW}])) by (le)) * 1000'
        ),
        "active_connections": (
            f'sum(http_connections_active{{{app_matchers}}})'
        ),
        "error_rate": (
            f'sum(rate(http_requests_total{{{app_matchers},status=~"4.*|5.*"}}[{RATE_WINDOW}])) / '
            f'sum(rate(http_requests_total{{{app_matchers}}}[{RATE_WINDOW}]))'
        ),
        "cpu_acceleration": (
            f'avg(rate(container_cpu_usage_seconds_total{{{resource_matchers}}}[{RATE_WINDOW}])) * 100'
            f' - avg(rate(container_cpu_usage_seconds_total{{{resource_matchers}}}[{BASELINE_WINDOW}])) * 100'
        ),
        "rps_acceleration": (
            f'sum(rate(http_requests_total{{{app_matchers}}}[{RATE_WINDOW}]))'
            f' - sum(rate(http_requests_total{{{app_matchers}}}[{BASELINE_WINDOW}]))'
        ),
        "current_replicas": (
            f'kube_deployment_status_replicas_ready{{deployment="{target_app}",namespace="{namespace}"}}'
        ),
    }
