"""
Kubernetes Tools Package
========================
Exports client factory, schemas, and tools for Kubernetes operations.
Mirrors nexus.tools.aws.
"""

from nexus.tools.k8s.client import get_k8s_client
from nexus.tools.k8s.schemas import (
    DescribeResourceSchema,
    GetMetricsSchema,
    GetPodLogsSchema,
    PatchResourceLimitsSchema,
    RemoveContainerCommandSchema,
    RestartDeploymentSchema,
    RollbackDeploymentSchema,
    ScaleResourceSchema,
)
from nexus.tools.k8s.tools import (
    K8sDescribeResourceTool,
    K8sGetMetricsTool,
    K8sGetPodLogsTool,
    K8sPatchResourceLimitsTool,
    K8sRemoveContainerCommandTool,
    K8sRestartDeploymentTool,
    K8sRollbackDeploymentTool,
    K8sScaleResourceTool,
)

__all__ = [
    "get_k8s_client",
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
