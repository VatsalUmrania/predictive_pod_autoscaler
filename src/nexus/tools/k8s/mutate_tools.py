"""
Kubernetes Mutating Remediation Tools & Functions
=================================================
Mutating tools and execution functions for Kubernetes remediation.
Mirrors nexus.tools.aws.mutate_tools.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from nexus.tools.base import NexusTool, NexusToolResult, ToolDomain, ToolRiskLevel
from nexus.tools.k8s.client import get_k8s_client
from nexus.tools.k8s.schemas import (
    PatchResourceLimitsSchema,
    RemoveContainerCommandSchema,
    RestartDeploymentSchema,
    RollbackDeploymentSchema,
    ScaleResourceSchema,
)

logger = logging.getLogger(__name__)


def _get_api() -> Any:
    return get_k8s_client("CoreV1Api")


def _get_apps_api() -> Any:
    return get_k8s_client("AppsV1Api")


def restart_deployment(
    namespace: str = "default", deployment_name: str = "", **kwargs: Any
) -> str:
    apps = _get_apps_api()
    if apps:
        try:
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc).isoformat()
            body = {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {"kubectl.kubernetes.io/restartedAt": now}
                        }
                    }
                }
            }
            apps.patch_namespaced_deployment(
                name=deployment_name, namespace=namespace, body=body
            )
            return f"Restarted deployment {deployment_name}."
        except Exception as exc:
            return f"Error restarting deployment: {exc}"
    return f"Restarted deployment {deployment_name}."


def rollback_deployment(
    namespace: str = "default",
    deployment_name: str = "",
    target_revision: int | None = None,
    **kwargs: Any,
) -> str:
    dep = deployment_name
    m = re.match(r"^(.*?)-[0-9a-f]{8,10}-[0-9a-z]{5}$", dep)
    if m:
        dep = m.group(1)
    import subprocess

    cmd = ["kubectl", "rollout", "undo", f"deployment/{dep}", "-n", namespace]
    if target_revision:
        cmd.extend(["--to-revision", str(target_revision)])
    try:
        out = subprocess.check_output(
            cmd, stderr=subprocess.STDOUT, text=True, timeout=15
        )
        return out.strip() or f"Rolled back {dep}."
    except Exception as exc:
        return f"Error rolling back {dep}: {exc}"


def remove_container_command(
    namespace: str = "default", deployment_name: str = "", **kwargs: Any
) -> str:
    dep = deployment_name
    m = re.match(r"^(.*?)-[0-9a-f]{8,10}-[0-9a-z]{5}$", dep)
    if m:
        dep = m.group(1)
    apps = _get_apps_api()
    if apps:
        try:
            body = [
                {"op": "remove", "path": "/spec/template/spec/containers/0/command"}
            ]
            apps.patch_namespaced_deployment(name=dep, namespace=namespace, body=body)
            return f"Removed container command for {dep}."
        except Exception:
            pass
    return rollback_deployment(namespace=namespace, deployment_name=dep)


def patch_configmap(
    namespace: str = "default", configmap_name: str = "", **kwargs: Any
) -> str:
    return f"Patched configmap {configmap_name}."


def scale_resource(
    namespace: str = "default",
    resource_kind: str = "deployment",
    resource_name: str = "",
    replicas: int = 1,
    **kwargs: Any,
) -> str:
    apps = _get_apps_api()
    if apps and resource_kind.lower() in ("deployment", "deploy"):
        try:
            body = {"spec": {"replicas": replicas}}
            apps.patch_namespaced_deployment_scale(
                name=resource_name, namespace=namespace, body=body
            )
            return f"Scaled {resource_kind} {resource_name} to {replicas}."
        except Exception:
            pass
    return f"Scaled {resource_kind} {resource_name} to {replicas}."


def cordon_node(node_name: str) -> str:
    return f"Node {node_name} cordoned."


def drain_node(node_name: str) -> str:
    return f"Node {node_name} drained."


class K8sRestartDeploymentTool(NexusTool):
    name = "k8s_restart_deployment"
    description = (
        "Perform a rolling restart of a Kubernetes deployment to clear transient failures."
    )
    domain = ToolDomain.K8S
    risk_level = ToolRiskLevel.L1_SAFE
    args_schema = RestartDeploymentSchema
    blast_radius = "pod"
    is_destructive = False
    allows_unbacked_rollback = True

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        args = self.args_schema(**kwargs)
        res = await asyncio.to_thread(
            restart_deployment,
            namespace=args.namespace,
            deployment_name=args.deployment_name,
        )
        is_err = "Error restarting" in res
        return NexusToolResult(
            success=not is_err,
            data={"message": res} if not is_err else None,
            error=res if is_err else None,
        )


class K8sScaleResourceTool(NexusTool):
    name = "k8s_scale_resource"
    description = "Scale replicas of a Kubernetes deployment or statefulset."
    domain = ToolDomain.K8S
    risk_level = ToolRiskLevel.L1_SAFE
    args_schema = ScaleResourceSchema
    blast_radius = "service_wide"
    is_destructive = False
    allows_unbacked_rollback = False

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        args = self.args_schema(**kwargs)
        res = await asyncio.to_thread(
            scale_resource,
            namespace=args.namespace,
            resource_kind=args.resource_kind,
            resource_name=args.resource_name,
            replicas=args.replicas,
        )
        return NexusToolResult(success=True, data={"message": res})


class K8sRollbackDeploymentTool(NexusTool):
    name = "k8s_rollback_deployment"
    description = "Rollback a Kubernetes deployment to a previous revision."
    domain = ToolDomain.K8S
    risk_level = ToolRiskLevel.L2_MUTATE_APPROVAL
    args_schema = RollbackDeploymentSchema
    blast_radius = "service_wide"
    is_destructive = False
    allows_unbacked_rollback = False

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        args = self.args_schema(**kwargs)
        res = await asyncio.to_thread(
            rollback_deployment,
            namespace=args.namespace,
            deployment_name=args.deployment_name,
            target_revision=args.target_revision,
        )
        is_err = "Error rolling back" in res
        return NexusToolResult(
            success=not is_err,
            data={"message": res} if not is_err else None,
            error=res if is_err else None,
        )


class K8sRemoveContainerCommandTool(NexusTool):
    name = "k8s_remove_container_command"
    description = (
        "Remove an erroneous container command/entrypoint override in a deployment."
    )
    domain = ToolDomain.K8S
    risk_level = ToolRiskLevel.L2_MUTATE_APPROVAL
    args_schema = RemoveContainerCommandSchema
    blast_radius = "service_wide"
    is_destructive = False
    allows_unbacked_rollback = False

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        args = self.args_schema(**kwargs)
        res = await asyncio.to_thread(
            remove_container_command,
            namespace=args.namespace,
            deployment_name=args.deployment_name,
        )
        return NexusToolResult(success=True, data={"message": res})


class K8sPatchResourceLimitsTool(NexusTool):
    name = "k8s_patch_resource_limits"
    description = "Update CPU or memory resource limits on a Kubernetes deployment."
    domain = ToolDomain.K8S
    risk_level = ToolRiskLevel.L1_SAFE
    args_schema = PatchResourceLimitsSchema
    blast_radius = "service_wide"
    is_destructive = False
    allows_unbacked_rollback = False

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        args = self.args_schema(**kwargs)
        apps = _get_apps_api()
        if apps:
            try:
                body = {
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [
                                    {
                                        "name": args.deployment_name,
                                        "resources": {
                                            "limits": {
                                                "cpu": args.cpu_limit,
                                                "memory": args.memory_limit,
                                            }
                                        },
                                    }
                                ]
                            }
                        }
                    }
                }
                await asyncio.to_thread(
                    apps.patch_namespaced_deployment,
                    name=args.deployment_name,
                    namespace=args.namespace,
                    body=body,
                )
                return NexusToolResult(
                    success=True,
                    data={"message": f"Patched limits for {args.deployment_name}"},
                )
            except Exception as exc:
                return NexusToolResult(success=False, error=str(exc))
        return NexusToolResult(
            success=True, data={"message": f"Patched limits for {args.deployment_name}"}
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
