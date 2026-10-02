"""
Kubernetes Tools Facade
=======================
Backward-compatibility facade for k8s tool functions.
"""

from nexus.agents.k8s_tools import (
    PROMETHEUS_URL,
    _get_api,
    _get_apps_api,
    cordon_node,
    describe_resource,
    drain_node,
    get_events,
    get_metrics,
    get_pod_logs,
    get_yaml,
    patch_configmap,
    remove_container_command,
    restart_deployment,
    rollback_deployment,
    scale_resource,
)

__all__ = [
    "PROMETHEUS_URL",
    "_get_api",
    "_get_apps_api",
    "get_pod_logs",
    "describe_resource",
    "get_events",
    "get_metrics",
    "get_yaml",
    "restart_deployment",
    "rollback_deployment",
    "remove_container_command",
    "patch_configmap",
    "scale_resource",
    "cordon_node",
    "drain_node",
]
