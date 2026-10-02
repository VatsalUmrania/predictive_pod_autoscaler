"""
NEXUS Kubernetes Platform Adapter
=================================
Platform adapter implementation for Kubernetes clusters.
Integrates with nexus.agents.k8s_tools, nexus.tools.k8s_adapter,
and LiveStateValidator.
"""

from __future__ import annotations

import logging
from typing import Any

from nexus.graph.platform.base import (
    BasePlatformAdapter,
    HealthCheckResult,
    LiveStateSnapshot,
    PlatformTelemetry,
    RollbackSnapshot,
    TargetResource,
)
from nexus.graph.platform.k8s_snapshot import capture_k8s_snapshot
from nexus.graph.platform.k8s_target import can_handle_k8s, detect_k8s_target
from nexus.graph.platform.k8s_telemetry import (
    collect_k8s_telemetry,
    inspect_k8s_live_state,
    verify_k8s_health,
)
from nexus.tools.base import NexusTool, NexusToolResult
from nexus.tools.k8s_adapter import (
    K8sDescribeResourceTool,
    K8sGetMetricsTool,
    K8sGetPodLogsTool,
    K8sPatchResourceLimitsTool,
    K8sRemoveContainerCommandTool,
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
            K8sRemoveContainerCommandTool.name: K8sRemoveContainerCommandTool(),
        }
        self._tools["k8s_scale_deployment"] = self._tools[K8sScaleResourceTool.name]
        self._tools["scale_deployment"] = self._tools[K8sScaleResourceTool.name]
        self._tools["restart_deployment"] = self._tools[K8sRestartDeploymentTool.name]
        self._tools["rollback_deployment"] = self._tools[K8sRollbackDeploymentTool.name]
        self._tools["k8s_restart"] = self._tools[K8sRestartDeploymentTool.name]
        self._tools["k8s_rollback"] = self._tools[K8sRollbackDeploymentTool.name]
        self._tools["remove_command_override"] = self._tools[K8sRemoveContainerCommandTool.name]
        self._tools["k8s_remove_command"] = self._tools[K8sRemoveContainerCommandTool.name]
        self._tools["k8s_clear_command"] = self._tools[K8sRemoveContainerCommandTool.name]

    @property
    def platform_id(self) -> str:
        return "kubernetes"

    def can_handle(self, event_or_target: dict[str, Any] | TargetResource) -> bool:
        return can_handle_k8s(event_or_target)

    def detect_target(self, events: list[dict[str, Any]]) -> TargetResource:
        return detect_k8s_target(events)

    async def collect_telemetry(self, target: TargetResource) -> PlatformTelemetry:
        return await collect_k8s_telemetry(target)

    async def inspect_live_state(self, target: TargetResource) -> LiveStateSnapshot:
        return await inspect_k8s_live_state(target)

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
        return await capture_k8s_snapshot(target, tool_name, parameters)

    async def execute_rollback(self, snapshot: RollbackSnapshot) -> NexusToolResult:
        logger.info("[K8sPlatformAdapter] Executing rollback snapshot %s", snapshot.snapshot_id)
        return await self.execute_action(
            snapshot.rollback_tool, snapshot.rollback_parameters
        )

    async def verify_health(
        self, target: TargetResource, plan_step: dict[str, Any]
    ) -> HealthCheckResult:
        return await verify_k8s_health(target, plan_step)
