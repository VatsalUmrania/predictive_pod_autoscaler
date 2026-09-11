"""
Tests demonstrating extensibility for FUTURE PLATFORMS (e.g. GCP, Azure, Bare-Metal).
Verifies that adding a new platform requires ONLY implementing BasePlatformAdapter
and registering with PlatformRegistry, requiring ZERO changes to the LangGraph core workflow.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from nexus.graph.platform import (
    BasePlatformAdapter,
    HealthCheckResult,
    LiveStateSnapshot,
    PlatformTelemetry,
    RollbackSnapshot,
    TargetResource,
)
from nexus.graph.workflow import IncidentWorkflow
from nexus.tools.base import NexusTool, NexusToolResult, ToolDomain, ToolRiskLevel


class GCPRestartInstanceSchema(BaseModel):
    project_id: str
    zone: str
    instance_name: str


class GCPRestartInstanceTool(NexusTool):
    name = "gcp_restart_compute_instance"
    description = "Restart a Google Cloud Compute Engine VM instance"
    domain = ToolDomain.SYSTEM
    risk_level = ToolRiskLevel.L1_SAFE
    args_schema = GCPRestartInstanceSchema

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        return NexusToolResult(success=True, data={"status": "TERMINATED -> PROVISIONING -> RUNNING"})


class MockGCPPlatformAdapter(BasePlatformAdapter):
    """Example future platform adapter for Google Cloud Platform."""

    def __init__(self) -> None:
        self._tools = {GCPRestartInstanceTool.name: GCPRestartInstanceTool()}
        self.restarted = False

    @property
    def platform_id(self) -> str:
        return "gcp"

    def can_handle(self, event_or_target: dict[str, Any] | TargetResource) -> bool:
        if isinstance(event_or_target, TargetResource):
            return event_or_target.platform.lower() in ("gcp", "google_cloud")
        if isinstance(event_or_target, dict):
            if event_or_target.get("platform") == "gcp":
                return True
            res = str(event_or_target.get("resource_name", ""))
            return res.startswith("projects/") or "gcp" in str(event_or_target.get("namespace", "")).lower()
        return False

    def detect_target(self, events: list[dict[str, Any]]) -> TargetResource:
        evt = events[0] if events else {}
        res = str(evt.get("resource_name", "projects/prod-cluster/zones/us-central1/instances/api-gw"))
        name = res.split("/")[-1]
        zone = res.split("/")[3] if len(res.split("/")) > 3 else "us-central1"
        return TargetResource(platform="gcp", namespace=zone, name=name, kind="compute_instance", arn_or_uri=res)

    async def collect_telemetry(self, target: TargetResource) -> PlatformTelemetry:
        return PlatformTelemetry(
            target=target,
            metrics={"cpu_utilization": 0.99, "disk_throttle": True},
            recent_logs=["GCP Compute VM memory pressure detected", "Instance unreachable: health check timeout"],
            health_status="failing",
        )

    async def inspect_live_state(self, target: TargetResource) -> LiveStateSnapshot:
        return LiveStateSnapshot(target=target, exists=True, is_failing=True)

    def get_tools(self) -> list[NexusTool]:
        return list(self._tools.values())

    async def execute_action(self, tool_name: str, parameters: dict[str, Any]) -> NexusToolResult:
        self.restarted = True
        return await self._tools[tool_name].execute(**parameters)

    async def capture_snapshot(self, target: TargetResource, tool_name: str, parameters: dict[str, Any]) -> RollbackSnapshot | None:
        return RollbackSnapshot(
            snapshot_id="gcp-snap-123",
            target=target,
            action_name=tool_name,
            rollback_tool=tool_name,
            rollback_parameters=parameters,
            description="Revert GCP Compute Instance restart",
        )

    async def execute_rollback(self, snapshot: RollbackSnapshot) -> NexusToolResult:
        return NexusToolResult(success=True, data={"rollback": "gcp_instance_state_reverted"})

    async def verify_health(self, target: TargetResource, plan_step: dict[str, Any]) -> HealthCheckResult:
        return HealthCheckResult(
            healthy=True,
            slo_restored=True,
            details="GCP Cloud Monitoring confirms instance status is RUNNING",
        )


@pytest.mark.asyncio
async def test_future_platform_registration_and_workflow_execution():
    """Verify that a future platform (GCP) plugs directly into the workflow and executes successfully."""
    from nexus.graph.platform import get_platform_registry

    registry = get_platform_registry()
    gcp_adapter = MockGCPPlatformAdapter()
    registry.register(gcp_adapter)

    # 1. Test Registry Resolution
    event = {
        "platform": "gcp",
        "resource_name": "projects/prod-proj/zones/us-central1-a/instances/payment-worker-v1",
        "severity": "critical",
        "signal_type": "gcp_vm_unresponsive",
    }
    resolved = registry.resolve_adapter(event)
    assert resolved.platform_id == "gcp"

    # 2. Test Full LangGraph Workflow with future GCP platform
    workflow = IncidentWorkflow()
    result = await workflow.run_incident(
        {
            "incident_id": "inc-gcp-future-001",
            "platform": "gcp",
            "events": [event],
            "approval_decision": "approved",  # Pre-approve if flagged
        }
    )

    # 3. Assert workflow reached terminal state and healed target
    assert result["incident_id"] == "inc-gcp-future-001"
    assert result["platform"] == "gcp"
    assert result["target"]["name"] == "payment-worker-v1"
    assert result["telemetry"]["health_status"] == "failing"
    assert result["resolved"] is True
    assert result["fsm_state"] == "resolved"
    assert len(result["audit_log"]) >= 5
    stages = [entry["stage"] for entry in result["audit_log"]]
    assert "triage" in stages
    assert "collect_telemetry" in stages
    assert "diagnose" in stages
    assert "plan" in stages
    assert "govern" in stages
    assert "execute" in stages
    assert "verify" in stages
    assert "learn" in stages
