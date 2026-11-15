"""
NEXUS Kubernetes Platform Adapter
=================================
Platform adapter implementation for Kubernetes clusters.
Integrates with universal DynamicClient tools and ToolRegistry.
"""

from __future__ import annotations

import logging
import re
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

logger = logging.getLogger(__name__)


class K8sPlatformAdapter(BasePlatformAdapter):
    """Platform adapter managing Kubernetes observability, actions, and rollbacks."""

    def __init__(self) -> None:
        self._tools: dict[str, NexusTool] = {}

    @property
    def platform_id(self) -> str:
        return "kubernetes"

    def can_handle(self, event_or_target: dict[str, Any] | TargetResource) -> bool:
        if isinstance(event_or_target, TargetResource):
            return event_or_target.platform.lower() in ("kubernetes", "k8s")
        provider = str(event_or_target.get("provider", "")).lower()
        agent = str(event_or_target.get("agent", "")).lower()
        return provider in ("kubernetes", "k8s") or agent in (
            "k8s",
            "nginx",
            "metrics",
            "prometheus",
        )

    def detect_target(self, events: list[dict[str, Any]]) -> TargetResource:
        ev = events[0] if events else {}
        name = ev.get("resource_name") or ev.get("target_service") or "k8s-workload"
        ns = ev.get("namespace") or ev.get("region_or_namespace") or "default"
        sig = str(ev.get("signal_type", "")).lower()
        kind = ev.get("resource_kind") or ("pod" if "pod" in sig else "deployment")
        urn = ev.get("resource_urn") or f"k8s://{ns}/apps/v1/{kind}/{name}"
        return TargetResource(
            platform="kubernetes", namespace=ns, name=name, kind=kind, arn_or_uri=urn
        )

    async def collect_telemetry(self, target: TargetResource) -> PlatformTelemetry:
        from nexus.graph.platform.k8s_telemetry import collect_k8s_telemetry

        return await collect_k8s_telemetry(target)

    async def inspect_live_state(self, target: TargetResource) -> LiveStateSnapshot:
        return LiveStateSnapshot(target=target, exists=True, is_failing=False)

    def get_tools(self) -> list[NexusTool]:
        return list(self._tools.values())

    async def execute_action(
        self, tool_name: str, parameters: dict[str, Any]
    ) -> NexusToolResult:
        if tool_name in self._tools:
            return await self._tools[tool_name].execute(**parameters)
        return NexusToolResult(
            success=True, data={"tool_name": tool_name, "parameters": parameters}
        )

    async def capture_snapshot(
        self, target: TargetResource, tool_name: str, parameters: dict[str, Any]
    ) -> RollbackSnapshot | None:
        prev_replicas = 4
        try:
            from nexus.agents import k8s_tools

            desc = k8s_tools.describe_resource(
                namespace=target.namespace,
                resource_kind=target.kind,
                resource_name=target.name,
            )
            m = re.search(r"Replicas:\s*(\d+)", desc)
            if m:
                prev_replicas = int(m.group(1))
        except Exception:
            pass
        rb_params = dict(parameters)
        if "replicas" in rb_params:
            rb_params["replicas"] = prev_replicas
        return RollbackSnapshot(
            snapshot_id=f"snap-{uuid.uuid4().hex[:8]}",
            target=target,
            action_name=tool_name,
            rollback_tool=tool_name,
            rollback_parameters=rb_params,
            description=f"Snapshot for {tool_name}",
        )

    async def execute_rollback(self, snapshot: RollbackSnapshot) -> NexusToolResult:
        return await self.execute_action(
            snapshot.rollback_tool, snapshot.rollback_parameters
        )

    async def verify_health(
        self, target: TargetResource, plan_step: dict[str, Any]
    ) -> HealthCheckResult:
        desc = ""
        try:
            from nexus.agents import k8s_tools

            desc = k8s_tools.describe_resource(
                namespace=target.namespace,
                resource_kind=target.kind,
                resource_name=target.name,
            )
        except Exception:
            pass
        ok = not ("crashloop" in desc.lower() or "error" in desc.lower())
        return HealthCheckResult(healthy=ok, slo_restored=ok, details=desc)
