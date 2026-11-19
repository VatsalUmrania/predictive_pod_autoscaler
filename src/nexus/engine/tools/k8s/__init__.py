"""
Kubernetes Tools Package
========================
Exports client factory, schemas, and tools for Kubernetes operations.
Mirrors nexus.engine.tools.aws.
"""

from nexus.engine.tools.k8s.client.client import get_k8s_client
from nexus.engine.tools.k8s.client.schemas import (
    DescribeResourceSchema,
    GetMetricsSchema,
    GetPodLogsSchema,
    PatchResourceLimitsSchema,
    RemoveContainerCommandSchema,
    RestartDeploymentSchema,
    RollbackDeploymentSchema,
    ScaleResourceSchema,
)
from nexus.engine.tools.k8s.mutate.tools import (
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
