"""
Kubernetes Mutating and Remediation Tools
"""

from __future__ import annotations

import asyncio
from typing import Any

from nexus.tools.base import NexusTool, NexusToolResult, ToolDomain, ToolRiskLevel
from nexus.tools.k8s.schemas import (
    PatchResourceLimitsSchema,
    RemoveContainerCommandSchema,
    RestartDeploymentSchema,
    RollbackDeploymentSchema,
    ScaleResourceSchema,
)


class K8sRestartDeploymentTool(NexusTool):
    name = "k8s_restart_deployment"
    description = "Initiate a rolling restart of a Kubernetes deployment."
    domain = ToolDomain.K8S
    risk_level = ToolRiskLevel.L1_SAFE
    args_schema = RestartDeploymentSchema
    blast_radius = "pod"
    is_destructive = False
    allows_unbacked_rollback = True

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        from nexus.agents import k8s_tools

        args = self.args_schema(**kwargs)
        res = await asyncio.to_thread(
            k8s_tools.restart_deployment,
            namespace=args.namespace,
            deployment_name=args.deployment_name,
        )
        is_err = "Error restarting deployment" in res
        return NexusToolResult(
            success=not is_err,
            data={"message": res} if not is_err else None,
            error=res if is_err else None,
        )


class K8sScaleResourceTool(NexusTool):
    name = "k8s_scale_resource"
    description = "Scale a Kubernetes deployment to the specified replica count."
    domain = ToolDomain.K8S
    risk_level = ToolRiskLevel.L2_MUTATE_APPROVAL
    args_schema = ScaleResourceSchema
    blast_radius = "service"
    is_destructive = False
    allows_unbacked_rollback = False

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        from nexus.agents import k8s_tools

        args = self.args_schema(**kwargs)
        res = await asyncio.to_thread(
            k8s_tools.scale_resource,
            namespace=args.namespace,
            resource_kind=args.resource_kind,
            resource_name=args.resource_name,
            replicas=args.replicas,
        )
        is_err = "Error scaling deployment" in res or "not supported" in res
        return NexusToolResult(
            success=not is_err,
            data={"message": res, "replicas": args.replicas} if not is_err else None,
            error=res if is_err else None,
        )

    def get_rollback_action(self, **kwargs: Any) -> dict[str, Any] | None:
        prev = kwargs.get("previous_replicas")
        if prev is not None:
            return {
                "tool_name": self.name,
                "parameters": {
                    "namespace": kwargs.get("namespace", "default"),
                    "resource_kind": kwargs.get("resource_kind", "deployment"),
                    "resource_name": kwargs.get("resource_name"),
                    "replicas": prev,
                },
            }
        return None


class K8sRollbackDeploymentTool(NexusTool):
    name = "k8s_rollback_deployment"
    description = (
        "Roll back a Kubernetes deployment to its previous stable ReplicaSet revision."
    )
    domain = ToolDomain.K8S
    risk_level = ToolRiskLevel.L2_MUTATE_APPROVAL
    args_schema = RollbackDeploymentSchema
    blast_radius = "service"
    is_destructive = False
    allows_unbacked_rollback = True

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        from nexus.agents import k8s_tools

        args = self.args_schema(**kwargs)
        res = await asyncio.to_thread(
            k8s_tools.rollback_deployment,
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
    name = "k8s_remove_command_override"
    description = "Remove container command override from a deployment."
    domain = ToolDomain.K8S
    risk_level = ToolRiskLevel.L2_MUTATE_APPROVAL
    args_schema = RemoveContainerCommandSchema
    blast_radius = "service"
    is_destructive = False
    allows_unbacked_rollback = False

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        from nexus.agents import k8s_tools

        args = self.args_schema(**kwargs)
        res = await asyncio.to_thread(
            k8s_tools.remove_container_command,
            namespace=args.namespace,
            deployment_name=args.deployment_name,
            container_name=args.container_name,
        )
        is_err = "Error removing" in res
        return NexusToolResult(
            success=not is_err,
            data={"message": res} if not is_err else None,
            error=res if is_err else None,
        )


class K8sPatchResourceLimitsTool(NexusTool):
    name = "k8s_patch_resource_limits"
    description = "Patch CPU/Memory limits for a deployment container."
    domain = ToolDomain.K8S
    risk_level = ToolRiskLevel.L2_MUTATE_APPROVAL
    args_schema = PatchResourceLimitsSchema
    blast_radius = "service"
    is_destructive = False
    allows_unbacked_rollback = False

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        from nexus.agents.k8s_tools import _get_apps_api

        args = self.args_schema(**kwargs)
        try:
            apps = _get_apps_api()
            limits = {}
            if args.cpu_limit:
                limits["cpu"] = args.cpu_limit
            if args.memory_limit:
                limits["memory"] = args.memory_limit
            patch = {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": args.container_name or args.deployment_name,
                                    "resources": {"limits": limits},
                                }
                            ]
                        }
                    }
                }
            }
            if apps:
                await asyncio.to_thread(
                    apps.patch_namespaced_deployment,
                    name=args.deployment_name,
                    namespace=args.namespace,
                    body=patch,
                )
            return NexusToolResult(
                success=True,
                data={
                    "message": f"Patched container limits to {limits}",
                    "limits": limits,
                },
            )
        except Exception as exc:
            return NexusToolResult(success=False, error=str(exc))
