"""
NEXUS Kubernetes Tools Adapter Facade
=====================================
Backward-compatibility facade for Kubernetes tools and schemas.
"""

from nexus.tools.k8s import (
    DescribeResourceSchema,
    GetMetricsSchema,
    GetPodLogsSchema,
    K8sDescribeResourceTool,
    K8sGetMetricsTool,
    K8sGetPodLogsTool,
    K8sPatchResourceLimitsTool,
    K8sRemoveContainerCommandTool,
    K8sRestartDeploymentTool,
    K8sRollbackDeploymentTool,
    K8sScaleResourceTool,
    PatchResourceLimitsSchema,
    RemoveContainerCommandSchema,
    RestartDeploymentSchema,
    RollbackDeploymentSchema,
    ScaleResourceSchema,
)

__all__ = [
    "GetPodLogsSchema",
    "DescribeResourceSchema",
    "GetMetricsSchema",
    "RestartDeploymentSchema",
    "ScaleResourceSchema",
    "RollbackDeploymentSchema",
    "RemoveContainerCommandSchema",
    "PatchResourceLimitsSchema",
    "K8sGetPodLogsTool",
    "K8sDescribeResourceTool",
    "K8sGetMetricsTool",
    "K8sRestartDeploymentTool",
    "K8sScaleResourceTool",
    "K8sRollbackDeploymentTool",
    "K8sRemoveContainerCommandTool",
    "K8sPatchResourceLimitsTool",
]
