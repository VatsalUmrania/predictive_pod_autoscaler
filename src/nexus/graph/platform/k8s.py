"""
NEXUS Kubernetes Platform Adapter
=================================
Platform adapter implementation for Kubernetes clusters.
Integrates with nexus.agents.k8s_tools, nexus.tools.k8s_adapter,
and LiveStateValidator.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from nexus.graph.platform.base import (
    BasePlatformAdapter,
    HealthCheckResult,
    LiveStateSnapshot,
    PlatformTelemetry,
    RollbackSnapshot,
    TargetResource,
)
from nexus.tools.base import NexusTool, NexusToolResult
from nexus.tools.k8s_adapter import (
    K8sDescribeResourceTool,
    K8sGetMetricsTool,
    K8sGetPodLogsTool,
    K8sPatchResourceLimitsTool,
    K8sRestartDeploymentTool,
    K8sRollbackDeploymentTool,
    K8sScaleResourceTool,
)

logger = logging.getLogger(__name__)


class K8sPlatformAdapter(BasePlatformAdapter):
    """Platform adapter managing Kubernetes observability, actions, and rollbacks."""

    def __init__(self) -> None:
        self._tools: dict[str, NexusTool] = {
            K8sGetPodLogsTool.name: K8sGetPodLogsTool(),
            K8sDescribeResourceTool.name: K8sDescribeResourceTool(),
            K8sGetMetricsTool.name: K8sGetMetricsTool(),
            K8sRestartDeploymentTool.name: K8sRestartDeploymentTool(),
            K8sScaleResourceTool.name: K8sScaleResourceTool(),
            K8sRollbackDeploymentTool.name: K8sRollbackDeploymentTool(),
            K8sPatchResourceLimitsTool.name: K8sPatchResourceLimitsTool(),
        }

    @property
    def platform_id(self) -> str:
        return "kubernetes"

    def can_handle(self, event_or_target: dict[str, Any] | TargetResource) -> bool:
        if isinstance(event_or_target, TargetResource):
            return event_or_target.platform.lower() in ("kubernetes", "k8s")

        if isinstance(event_or_target, dict):
            plat = str(event_or_target.get("platform", "")).lower()
            if plat in ("kubernetes", "k8s"):
                return True
            agent = str(event_or_target.get("agent", "")).lower()
            if agent in ("k8s", "metrics", "nginx", "git", "config", "db", "network"):
                return True
            res_name = str(event_or_target.get("resource_name", ""))
            if res_name.startswith("arn:aws:"):
                return False
            if "aws" in str(event_or_target.get("namespace", "")).lower():
                return False
            # Default to true if not explicitly AWS/GCP
            if agent not in ("lambda", "apigw", "sqs", "dynamodb", "cloudwatch"):
                return True

        return False

    def detect_target(self, events: list[dict[str, Any]]) -> TargetResource:
        if not events:
            return TargetResource(platform="kubernetes", namespace="default", name="unknown", kind="deployment")

        # Pick the most severe event or the first
        primary_evt = events[0]
        for evt in events:
            if str(evt.get("severity", "")).lower() == "critical":
                primary_evt = evt
                break

        raw_name = str(primary_evt.get("resource_name", "unknown"))
        namespace = str(primary_evt.get("namespace", "default"))
        kind = "deployment"

        # Handle namespace/name syntax if present
        if "/" in raw_name and not raw_name.startswith("arn:"):
            parts = raw_name.split("/", 1)
            namespace = parts[0]
            raw_name = parts[1]

        if "pod" in raw_name or "pod" in str(primary_evt.get("signal_type", "")).lower():
            kind = "pod"

        return TargetResource(
            platform="kubernetes",
            namespace=namespace or "default",
            name=raw_name,
            kind=kind,
        )

    async def collect_telemetry(self, target: TargetResource) -> PlatformTelemetry:
        """Deterministically collect logs, describe info, and live config without ReAct."""
        from nexus.agents import k8s_tools

        recent_logs: list[str] = []
        live_config: dict[str, Any] = {}
        health_status = "degraded"

        try:
            # 1. Gather describe information
            desc_res = await asyncio.to_thread(
                k8s_tools.describe_resource,
                namespace=target.namespace,
                resource_kind=target.kind,
                resource_name=target.name,
            )
            live_config["describe"] = desc_res

            # 2. Gather logs if target is a pod or deployment
            log_res = await asyncio.to_thread(
                k8s_tools.get_pod_logs,
                namespace=target.namespace,
                pod_name=target.name,
                tail_lines=50,
            )
            if "Error fetching logs" not in log_res:
                recent_logs.extend(log_res.splitlines()[-30:])
            else:
                recent_logs.append(log_res)

            # 3. Analyze health status from describe
            lower_desc = desc_res.lower()
            if "crashloopbackoff" in lower_desc:
                health_status = "crashloopbackoff"
            elif "oomkilled" in lower_desc:
                health_status = "oomkilled"
            elif "pending" in lower_desc:
                health_status = "pending"
            elif "running" in lower_desc:
                health_status = "running"
        except Exception as exc:
            logger.warning("[K8sPlatformAdapter] Telemetry gathering error for %s: %s", target, exc)
            live_config["error"] = str(exc)

        return PlatformTelemetry(
            target=target,
            metrics={"health": health_status},
            recent_logs=recent_logs,
            live_config=live_config,
            health_status=health_status,
        )

    async def inspect_live_state(self, target: TargetResource) -> LiveStateSnapshot:
        """Inspect live state before applying mutations."""
        from nexus.agents import k8s_tools

        try:
            desc = await asyncio.to_thread(
                k8s_tools.describe_resource,
                namespace=target.namespace,
                resource_kind=target.kind,
                resource_name=target.name,
            )
            not_found = "not found" in desc.lower() or "error" in desc.lower()
            is_failing = any(m in desc.lower() for m in ("crashloopbackoff", "oomkilled", "error", "unhealthy"))

            return LiveStateSnapshot(
                target=target,
                exists=not not_found,
                is_failing=is_failing,
                failure_details=desc[:300] if is_failing else None,
                state_attributes={"describe_preview": desc[:500]},
            )
        except Exception as exc:
            return LiveStateSnapshot(
                target=target,
                exists=False,
                is_failing=True,
                failure_details=str(exc),
            )

    def get_tools(self) -> list[NexusTool]:
        return list(self._tools.values())

    async def execute_action(
        self, tool_name: str, parameters: dict[str, Any]
    ) -> NexusToolResult:
        tool = self._tools.get(tool_name)
        if not tool:
            return NexusToolResult(
                success=False,
                error=f"Tool '{tool_name}' not registered in K8sPlatformAdapter",
            )
        return await tool.execute(**parameters)

    async def capture_snapshot(
        self, target: TargetResource, tool_name: str, parameters: dict[str, Any]
    ) -> RollbackSnapshot | None:
        """Capture rollback snapshot before mutation."""
        from nexus.agents import k8s_tools

        snap_id = str(uuid.uuid4())[:8]
        if tool_name == "k8s_scale_resource":
            desc = await asyncio.to_thread(
                k8s_tools.describe_resource,
                namespace=target.namespace,
                resource_kind=parameters.get("resource_kind", "deployment"),
                resource_name=parameters.get("resource_name", target.name),
            )
            # Default prior replicas to 1 if unknown
            old_replicas = 1
            import re
            m = re.search(r"Replicas:\s*(\d+)", desc)
            if m:
                old_replicas = int(m.group(1))

            return RollbackSnapshot(
                snapshot_id=f"k8s-scale-{snap_id}",
                target=target,
                action_name=tool_name,
                rollback_tool="k8s_scale_resource",
                rollback_parameters={
                    "namespace": target.namespace,
                    "resource_kind": parameters.get("resource_kind", "deployment"),
                    "resource_name": parameters.get("resource_name", target.name),
                    "replicas": old_replicas,
                },
                pre_mutation_state={"replicas": old_replicas},
                description=f"Rollback scale to {old_replicas} replicas",
            )

        if tool_name in ("k8s_restart_deployment", "k8s_rollback_deployment"):
            return RollbackSnapshot(
                snapshot_id=f"k8s-undo-{snap_id}",
                target=target,
                action_name=tool_name,
                rollback_tool="k8s_rollback_deployment",
                rollback_parameters={
                    "namespace": target.namespace,
                    "deployment_name": parameters.get("deployment_name", target.name),
                },
                description=f"Rollback deployment revision for {target.name}",
            )

        return None

    async def execute_rollback(self, snapshot: RollbackSnapshot) -> NexusToolResult:
        logger.info("[K8sPlatformAdapter] Executing rollback snapshot %s", snapshot.snapshot_id)
        return await self.execute_action(
            snapshot.rollback_tool, snapshot.rollback_parameters
        )

    async def verify_health(
        self, target: TargetResource, plan_step: dict[str, Any]
    ) -> HealthCheckResult:
        """Verify that the target deployment/pod recovered post-remediation."""
        from nexus.agents import k8s_tools

        try:
            desc = await asyncio.to_thread(
                k8s_tools.describe_resource,
                namespace=target.namespace,
                resource_kind=target.kind,
                resource_name=target.name,
            )
            lower_desc = desc.lower()
            is_unhealthy = any(
                bad in lower_desc
                for bad in ("crashloopbackoff", "oomkilled", "error", "evicted", "failed")
            )
            # In test environments or when mock is used
            healthy = not is_unhealthy
            return HealthCheckResult(
                healthy=healthy,
                slo_restored=healthy,
                failure_reason=None if healthy else "Unhealthy container status detected in describe",
                details=desc[:200],
            )
        except Exception as exc:
            return HealthCheckResult(
                healthy=False,
                slo_restored=False,
                failure_reason=str(exc),
            )
