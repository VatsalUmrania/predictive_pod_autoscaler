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
        from nexus.tools.registry import default_registry

        reg_tool = default_registry.get(tool_name)
        if reg_tool:
            return await default_registry.execute_tool(tool_name, parameters)
        return NexusToolResult(
            success=True, data={"tool_name": tool_name, "parameters": parameters}
        )

    async def capture_snapshot(
        self, target: TargetResource, tool_name: str, parameters: dict[str, Any]
    ) -> RollbackSnapshot | None:
        prev_replicas = int(parameters.get("replicas", 1))
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
        from nexus.agents import k8s_tools

        dep_name = None
        if target.kind.lower() in ("pod", "po"):
            m = re.match(r"^(.*?)-[0-9a-f]{8,10}-[0-9a-z]{5}$", target.name)
            if m:
                dep_name = m.group(1)

        desc = ""
        try:
            desc = k8s_tools.describe_resource(
                namespace=target.namespace,
                resource_kind=target.kind,
                resource_name=target.name,
            )
        except Exception as exc:
            desc = f"Error: {exc}"

        if dep_name and (
            not desc or desc.startswith("Error") or "not found" in desc.lower()
        ):
            try:
                dep_desc = k8s_tools.describe_resource(
                    namespace=target.namespace,
                    resource_kind="deployment",
                    resource_name=dep_name,
                )
                if dep_desc and not dep_desc.startswith("Error"):
                    dl = dep_desc.lower()
                    bad = any(
                        b in dl
                        for b in (
                            "crashloop",
                            "error",
                            "backoff",
                            "unavailable=1",
                            "unavailable=2",
                            "ready=0/",
                        )
                    )
                    if not bad and any(
                        ok in dl
                        for ok in ("1 available", "available", "ready: 1", "ready")
                    ):
                        return HealthCheckResult(
                            healthy=True,
                            slo_restored=True,
                            details=f"Parent deployment {dep_name} healthy",
                        )
            except Exception:
                pass

        if not desc or desc.startswith("Error"):
            return HealthCheckResult(
                healthy=False, slo_restored=False, details=desc or "Empty describe"
            )

        d_lower = desc.lower()
        has_failure = any(
            bad in d_lower
            for bad in (
                "crashloop",
                "error",
                "backoff",
                "ready=0/",
                "unavailable=1",
                "unavailable=2",
            )
        )
        if has_failure:
            return HealthCheckResult(healthy=False, slo_restored=False, details=desc)

        if dep_name:
            dep_desc = k8s_tools.describe_resource(
                namespace=target.namespace,
                resource_kind="deployment",
                resource_name=dep_name,
            )
            if any(
                bad in dep_desc.lower()
                for bad in ("unavailable=1", "unavailable=2", "ready=0", "error")
            ):
                return HealthCheckResult(
                    healthy=False, slo_restored=False, details=f"{desc} | {dep_desc}"
                )

        return HealthCheckResult(healthy=True, slo_restored=True, details=desc)
