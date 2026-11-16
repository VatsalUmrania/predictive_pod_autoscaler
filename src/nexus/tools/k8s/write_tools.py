"""
Backward-compatibility shim re-exporting from nexus.tools.k8s.mutate_tools.
"""

from nexus.tools.k8s.mutate_tools import (
    K8sPatchResourceLimitsTool,
    K8sRemoveContainerCommandTool,
    K8sRestartDeploymentTool,
    K8sRollbackDeploymentTool,
    K8sScaleResourceTool,
    _get_api,
    _get_apps_api,
    cordon_node,
    drain_node,
    patch_configmap,
    remove_container_command,
    restart_deployment,
    rollback_deployment,
    scale_resource,
)

__all__ = [
    "_get_api",
    "_get_apps_api",
    "restart_deployment",
    "rollback_deployment",
    "remove_container_command",
    "patch_configmap",
    "scale_resource",
    "cordon_node",
    "drain_node",
    "K8sRestartDeploymentTool",
    "K8sScaleResourceTool",
    "K8sRollbackDeploymentTool",
    "K8sRemoveContainerCommandTool",
    "K8sPatchResourceLimitsTool",
]
