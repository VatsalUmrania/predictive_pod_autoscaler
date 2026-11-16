"""
Backward-compatibility shim re-exporting from nexus.tools.k8s.observe_tools.
"""

from nexus.tools.k8s.observe_tools import (
    K8sDescribeResourceTool,
    K8sGetMetricsTool,
    K8sGetPodLogsTool,
    _get_api,
    _get_apps_api,
    describe_resource,
    get_events,
    get_metrics,
    get_pod_logs,
    get_yaml,
)

__all__ = [
    "_get_api",
    "_get_apps_api",
    "get_pod_logs",
    "describe_resource",
    "get_events",
    "get_metrics",
    "get_yaml",
    "K8sGetPodLogsTool",
    "K8sDescribeResourceTool",
    "K8sGetMetricsTool",
]
