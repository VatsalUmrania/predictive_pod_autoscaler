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
    description = "Initiate a rolling restart of a Kubernetes deployment (L1 safe automated action)."
    domain = ToolDomain.K8S
    risk_level = ToolRiskLevel.L1_SAFE
    args_schema = RestartDeploymentSchema

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
        prev_replicas = kwargs.get("previous_replicas")
        if prev_replicas is not None:
            return {
                "tool_name": self.name,
                "parameters": {
                    "namespace": kwargs.get("namespace", "default"),
                    "resource_kind": kwargs.get("resource_kind", "deployment"),
                    "resource_name": kwargs.get("resource_name"),
                    "replicas": prev_replicas,
                },
            }
        return None


class K8sRollbackDeploymentTool(NexusTool):
    name = "k8s_rollback_deployment"
    description = "Roll back a Kubernetes deployment to its previous stable ReplicaSet revision."
    domain = ToolDomain.K8S
    risk_level = ToolRiskLevel.L3_DESTRUCTIVE
    args_schema = RollbackDeploymentSchema

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        from nexus.agents import k8s_tools
        args = self.args_schema(**kwargs)
        res = await asyncio.to_thread(
            k8s_tools.rollback_deployment,
            namespace=args.namespace,
            deployment_name=args.deployment_name,
            target_revision=args.target_revision,
        )
        is_err = "Error rolling back" in res or "No previous ReplicaSet" in res
        return NexusToolResult(
            success=not is_err,
            data={"message": res} if not is_err else None,
            error=res if is_err else None,
        )


class K8sRemoveContainerCommandTool(NexusTool):
    name = "k8s_remove_command_override"
    description = "Remove faulty container command/args override from a deployment to restore container entrypoint."
    domain = ToolDomain.K8S
    risk_level = ToolRiskLevel.L2_MUTATE_APPROVAL
    args_schema = RemoveContainerCommandSchema

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        from nexus.agents import k8s_tools
        args = self.args_schema(**kwargs)
        res = await asyncio.to_thread(
            k8s_tools.remove_container_command,
            namespace=args.namespace,
            deployment_name=args.deployment_name,
            container_name=args.container_name,
        )
        is_err = "Error removing" in res or "not found" in res
        return NexusToolResult(
            success=not is_err,
            data={"message": res} if not is_err else None,
            error=res if is_err else None,
        )


class K8sPatchResourceLimitsTool(NexusTool):
    name = "k8s_patch_resource_limits"
    description = "Patch CPU/Memory requests or limits for a deployment container."
    domain = ToolDomain.K8S
    risk_level = ToolRiskLevel.L2_MUTATE_APPROVAL
    args_schema = PatchResourceLimitsSchema

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        from nexus.agents.k8s_tools import _get_apps_api
        args = self.args_schema(**kwargs)
        try:
            apps = _get_apps_api()
            dep = await asyncio.to_thread(
                apps.read_namespaced_deployment,
                name=args.deployment_name,
                namespace=args.namespace,
            )
            containers = dep.spec.template.spec.containers
            if not containers:
                return NexusToolResult(success=False, error="No containers found in deployment spec")

            target_c = containers[0]
            if args.container_name:
                for c in containers:
                    if c.name == args.container_name:
                        target_c = c
                        break

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
                                    "name": target_c.name,
                                    "resources": {"limits": limits},
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
                body=patch,
            )
            return NexusToolResult(
                success=True,
                data={
                    "message": f"Patched container {target_c.name} limits to {limits}",
                    "limits": limits,
                },
            )
        except Exception as exc:
            return NexusToolResult(success=False, error=str(exc))
