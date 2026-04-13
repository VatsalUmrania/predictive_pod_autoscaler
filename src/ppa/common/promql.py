# """PromQL query builders shared by the data collector and operator."""

# RATE_WINDOW = "5m"
# LATENCY_WINDOW = "5m"
# BASELINE_WINDOW = "5m"


# def _base_matchers(target_app: str, namespace: str) -> str:
#     """Single source of truth for pod matching."""
#     return f'pod=~"{target_app}-.*",namespace="{namespace}"'


# def _usage_matchers(target_app: str, namespace: str) -> str:
#     """Matchers for container usage (exclude junk containers)."""
#     base = _base_matchers(target_app, namespace)
#     return f'{base},container!="",container!="POD"'


# def _resource_matchers(target_app: str, namespace: str, resource: str) -> str:
#     """Matchers for resource limits."""
#     base = _base_matchers(target_app, namespace)
#     return f'{base},resource="{resource}"'


# def build_queries(
#     target_app: str, namespace: str, container_name: str | None = None
# ) -> dict[str, str]:
#     """Return PromQL for all queried features."""
#     base = _base_matchers(target_app, namespace)
#     usage = _usage_matchers(target_app, namespace)
#     cpu_res = _resource_matchers(target_app, namespace, "cpu")
#     mem_res = _resource_matchers(target_app, namespace, "memory")
#     deploy_matchers = f'deployment="{target_app}",namespace="{namespace}"'

#     # Rate window as literal to avoid f-string issues
#     rate_win = "5m"

#     return {
#         "requests_per_second": "(sum(rate(http_requests_total{"
#         + base
#         + "}["
#         + rate_win
#         + "])) or on() vector(0)",
#         "cpu_utilization_pct": (
#             "(sum(rate(container_cpu_usage_seconds_total{" + usage + "}[" + rate_win + "])) "
#             "/ clamp_min(sum(kube_pod_container_resource_limits{" + cpu_res + "}), 1e-6) * 100) "
#             "or on() vector(0)"
#         ),
#         "memory_utilization_pct": (
#             "(sum(container_memory_working_set_bytes{" + usage + "}) "
#             "/ clamp_min(sum(kube_pod_container_resource_limits{" + mem_res + "}), 1e-6) * 100) "
#             "or on() vector(0)"
#         ),
#         "latency_p95_ms": (
#             "(histogram_quantile(0.95, sum(rate("
#             "http_request_duration_seconds_bucket{"
#             + base
#             + "}["
#             + rate_win
#             + "])) by (le)) * 1000) or on() vector(0)"
#         ),
#         "active_connections": "(sum(http_connections_active{" + base + "}) or on() vector(0))",
#         "error_rate": (
#             "(sum(rate(http_requests_total{"
#             + base
#             + ',status=~"4.*|5.*"'
#             + "}["
#             + rate_win
#             + "])) "
#             "/ clamp_min(sum(rate(http_requests_total{" + base + "}[" + rate_win + "])), 1e-6)) "
#             "or on() vector(0)"
#         ),
#         "cpu_acceleration": (
#             "(sum(rate(container_cpu_usage_seconds_total{" + usage + "}[" + rate_win + "])) "
#             "/ clamp_min(sum(kube_pod_container_resource_limits{" + cpu_res + "}), 1e-6) * 100) "
#             "- (sum(rate(container_cpu_usage_seconds_total{" + usage + "}[" + rate_win + "])) "
#             "/ clamp_min(sum(kube_pod_container_resource_limits{" + cpu_res + "}), 1e-6) * 100)"
#         ),
#         "rps_acceleration": (
#             "(sum(rate(http_requests_total{" + base + "}[" + rate_win + "])) "
#             "/ clamp_min(sum(kube_deployment_status_replicas_ready{" + deploy_matchers + "}), 1)) "
#             "- (sum(rate(http_requests_total{" + base + "}[" + rate_win + "])) "
#             "/ clamp_min(sum(kube_deployment_status_replicas_ready{" + deploy_matchers + "}), 1))"
#         ),
#         "current_replicas": "sum(kube_deployment_status_replicas_ready{" + deploy_matchers + "})",
#     }


# def build_fallback_queries(
#     target_app: str, namespace: str, container_name: str | None = None
# ) -> dict[str, str]:
#     """Absolute queries for environments without resource limits."""
#     base = _base_matchers(target_app, namespace)
#     usage = _usage_matchers(target_app, namespace)

#     # Build queries using direct string concatenation to avoid escaping issues
#     return {
#         "cpu_core_percent": (
#             "(sum(rate(container_cpu_usage_seconds_total{"
#             + usage
#             + "}[5m])) or on() vector(0) * 100"
#         ),
#         "memory_usage_bytes": (
#             "(sum(container_memory_working_set_bytes{" + usage + "}) or on() vector(0)"
#         ),
#         "cpu_acceleration": (
#             "(sum(rate(container_cpu_usage_seconds_total{"
#             + usage
#             + "}[5m])) or on() vector(0) * 100 - "
#             "(sum(rate(container_cpu_usage_seconds_total{"
#             + usage
#             + "}[5m])) or on() vector(0) * 100"
#         ),
#     }

"""PromQL query builders shared by the data collector and operator."""

RATE_WINDOW = "5m"
LATENCY_WINDOW = "5m"
BASELINE_WINDOW = "15m"  # must be different from RATE_WINDOW


# ---------- Matchers ----------


def _base_matchers(target_app: str, namespace: str) -> str:
    """Pod-level matching (safe regex)."""
    return f'pod=~"{target_app}-.*",namespace="{namespace}"'


def _usage_matchers(target_app: str, namespace: str) -> str:
    """Exclude junk containers (pause, empty)."""
    base = _base_matchers(target_app, namespace)
    return f'{base},container!="",container!="POD"'


def _resource_matchers(target_app: str, namespace: str, resource: str) -> str:
    """Resource limits (cpu/memory)."""
    base = _base_matchers(target_app, namespace)
    return f'{base},resource="{resource}"'


# ---------- Main Queries ----------


def build_queries(
    target_app: str, namespace: str, container_name: str | None = None
) -> dict[str, str]:
    """Return PromQL for all queried features."""

    base = _base_matchers(target_app, namespace)
    usage = _usage_matchers(target_app, namespace)
    cpu_res = _resource_matchers(target_app, namespace, "cpu")
    mem_res = _resource_matchers(target_app, namespace, "memory")
    deploy = f'deployment="{target_app}",namespace="{namespace}"'

    return {
        # -------- Traffic --------
        "requests_per_second": (f"sum(rate(http_requests_total{{{base}}}[{RATE_WINDOW}]))"),
        # -------- CPU --------
        "cpu_utilization_pct": (
            f"sum(rate(container_cpu_usage_seconds_total{{{usage}}}[{RATE_WINDOW}])) "
            f"/ clamp_min(sum(kube_pod_container_resource_limits{{{cpu_res}}}), 1e-6) * 100"
        ),
        # -------- Memory --------
        "memory_utilization_pct": (
            f"sum(container_memory_working_set_bytes{{{usage}}}) "
            f"/ clamp_min(sum(kube_pod_container_resource_limits{{{mem_res}}}), 1e-6) * 100"
        ),
        # -------- Latency (safe fallback allowed) --------
        "latency_p95_ms": (
            f"(histogram_quantile(0.95, "
            f"sum(rate(http_request_duration_seconds_bucket{{{base}}}[{LATENCY_WINDOW}])) by (le)) "
            f"* 1000) or vector(0)"
        ),
        # -------- Connections --------
        "active_connections": (f"sum(http_connections_active{{{base}}})"),
        # -------- Error rate --------
        "error_rate": (
            f'sum(rate(http_requests_total{{{base},status=~"4.*|5.*"}}[{RATE_WINDOW}])) '
            f"/ clamp_min(sum(rate(http_requests_total{{{base}}}[{RATE_WINDOW}])), 1e-6)"
        ),
        # -------- CPU acceleration --------
        "cpu_acceleration": (
            f"(sum(rate(container_cpu_usage_seconds_total{{{usage}}}[{RATE_WINDOW}])) "
            f"/ clamp_min(sum(kube_pod_container_resource_limits{{{cpu_res}}}), 1e-6) * 100) "
            f"- (sum(rate(container_cpu_usage_seconds_total{{{usage}}}[{BASELINE_WINDOW}])) "
            f"/ clamp_min(sum(kube_pod_container_resource_limits{{{cpu_res}}}), 1e-6) * 100)"
        ),
        # -------- RPS acceleration --------
        "rps_acceleration": (
            f"(sum(rate(http_requests_total{{{base}}}[{RATE_WINDOW}])) "
            f"/ clamp_min(sum(kube_deployment_status_replicas_ready{{{deploy}}}), 1)) "
            f"- (sum(rate(http_requests_total{{{base}}}[{BASELINE_WINDOW}])) "
            f"/ clamp_min(sum(kube_deployment_status_replicas_ready{{{deploy}}}), 1))"
        ),
        # -------- Replicas --------
        "current_replicas": (f"sum(kube_deployment_status_replicas_ready{{{deploy}}})"),
    }


# ---------- Fallback Queries (ONLY when limits missing) ----------


def build_fallback_queries(
    target_app: str, namespace: str, container_name: str | None = None
) -> dict[str, str]:
    """
    Absolute queries for environments WITHOUT resource limits.
    No fake defaults. No masking.
    """

    usage = _usage_matchers(target_app, namespace)

    return {
        "cpu_core_usage": (
            f"sum(rate(container_cpu_usage_seconds_total{{{usage}}}[{RATE_WINDOW}]))"
        ),
        "cpu_core_percent": (
            f"sum(rate(container_cpu_usage_seconds_total{{{usage}}}[{RATE_WINDOW}])) * 100"
        ),
        "memory_usage_bytes": (f"sum(container_memory_working_set_bytes{{{usage}}})"),
        "memory_utilization_pct": (f"sum(container_memory_working_set_bytes{{{usage}}})"),
        "cpu_acceleration": (
            f"sum(rate(container_cpu_usage_seconds_total{{{usage}}}[{RATE_WINDOW}])) "
            f"- sum(rate(container_cpu_usage_seconds_total{{{usage}}}[{BASELINE_WINDOW}]))"
        ),
    }
