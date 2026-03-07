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
    # kubelet/cAdvisor container-level usage metrics (e.g. cpu_usage_seconds_total) 
    # often lack a 'container' label on pod-aggregate series or in certain K8s configs.
    # We use app_matchers (pod-level) for usage and resource_matchers (container-level) for limits.
    resource_matchers = _matchers(target_app, namespace, container_name)

    return {
        "requests_per_second": (
            f'sum(rate(http_requests_total{{{app_matchers}}}[{RATE_WINDOW}]))'
        ),
        "cpu_utilization_pct": (
            f'sum(rate(container_cpu_usage_seconds_total{{{app_matchers}}}[{RATE_WINDOW}])) '
            f'/ sum(kube_pod_container_resource_limits{{resource="cpu", {resource_matchers}}}) * 100'
        ),
        "memory_utilization_pct": (
            f'sum(container_memory_working_set_bytes{{{app_matchers}}}) '
            f'/ sum(kube_pod_container_resource_limits{{resource="memory", {resource_matchers}}}) * 100'
        ),
        "latency_p95_ms": (
            f'(histogram_quantile(0.95, sum(rate('
            f'http_request_duration_seconds_bucket{{{app_matchers}}}[{LATENCY_WINDOW}])) by (le)) * 1000) or on() vector(0)'
        ),
        "active_connections": (
            f'sum(http_connections_active{{{app_matchers}}})'
        ),
        "error_rate": (
            f'(sum(rate(http_requests_total{{{app_matchers},status=~"4.*|5.*"}}[{RATE_WINDOW}])) / '
            f'sum(rate(http_requests_total{{{app_matchers}}}[{RATE_WINDOW}]))) or on() vector(0)'
        ),
        "cpu_acceleration": (
            f'(sum(rate(container_cpu_usage_seconds_total{{{app_matchers}}}[{RATE_WINDOW}])) '
            f'/ sum(kube_pod_container_resource_limits{{resource="cpu", {resource_matchers}}}) * 100) '
            f'- (sum(rate(container_cpu_usage_seconds_total{{{app_matchers}}}[{BASELINE_WINDOW}])) '
            f'/ sum(kube_pod_container_resource_limits{{resource="cpu", {resource_matchers}}}) * 100)'
        ),
        "rps_acceleration": (
            f'(sum(rate(http_requests_total{{{app_matchers}}}[{RATE_WINDOW}])) / kube_deployment_status_replicas_ready{{deployment="{target_app}",namespace="{namespace}"}}) '
            f'- (sum(rate(http_requests_total{{{app_matchers}}}[{BASELINE_WINDOW}])) / kube_deployment_status_replicas_ready{{deployment="{target_app}",namespace="{namespace}"}})'
        ),
        "current_replicas": (
            f'kube_deployment_status_replicas_ready{{deployment="{target_app}",namespace="{namespace}"}}'
        ),
    }


def build_fallback_queries(target_app: str, namespace: str, container_name: str | None = None) -> dict[str, str]:
    """Return absolute equivalent queries for environments without resource limits."""
    app_matchers = _matchers(target_app, namespace)
    return {
        "cpu_core_percent": f'sum(rate(container_cpu_usage_seconds_total{{{app_matchers}}}[{RATE_WINDOW}])) * 100',
        "memory_usage_bytes": f'sum(container_memory_working_set_bytes{{{app_matchers}}})',
        "cpu_acceleration": (
            f'sum(rate(container_cpu_usage_seconds_total{{{app_matchers}}}[{RATE_WINDOW}])) * 100 '
            f'- sum(rate(container_cpu_usage_seconds_total{{{app_matchers}}}[{BASELINE_WINDOW}])) * 100'
        )
    }
