"""
Tests demonstrating extensibility for FUTURE PLATFORMS (e.g. GCP, Azure, Bare-Metal).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import BaseModel

from nexus.graph.platform import (
    BasePlatformAdapter,
    HealthCheckResult,
    LiveStateSnapshot,
    PlatformTelemetry,
    RollbackSnapshot,
    TargetResource,
    get_platform_registry,
)
from nexus.graph.workflow import IncidentWorkflow
from nexus.tools.base import NexusTool, NexusToolResult, ToolDomain, ToolRiskLevel


class GCPRestartInstanceSchema(BaseModel):
    project_id: str = ""
    zone: str = ""
    instance_name: str = ""


class GCPRestartInstanceTool(NexusTool):
    name = "gcp_restart_compute_instance"
    description = "Restart a Google Cloud Compute Engine VM instance"
    domain = ToolDomain.SYSTEM
    risk_level = ToolRiskLevel.L1_SAFE
    args_schema = GCPRestartInstanceSchema

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        return NexusToolResult(
            success=True, data={"status": "TERMINATED -> PROVISIONING -> RUNNING"}
        )


class MockFutureGCPModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "mock-future-gcp"

    def _generate(self, *args: Any, **kwargs: Any) -> ChatResult:
        msg = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "RCAReport",
                    "args": {
                        "root_cause_summary": "GCP Compute VM memory pressure detected",
                        "affected_resource_urn": "projects/prod-proj/zones/us-central1-a/instances/payment-worker-v1",
                        "provider": "gcp",
                        "confidence": 0.95,
                        "hypotheses_evaluated": ["memory_pressure"],
                        "evidence_chain": [
                            {
                                "tool_name": "gcp_get_telemetry",
                                "tool_arguments": {},
                                "key_finding": "Unreachable",
                            }
                        ],
                        "pre_incident_resource_state": {"status": "RUNNING"},
                        "requires_remediation": True,
                        "suggested_action": "gcp_restart_compute_instance",
                    },
                    "id": "rca_wf_gcp_1",
                }
            ],
        )
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def bind_tools(self, *args: Any, **kwargs: Any) -> Any:
        return self


class MockGCPPlatformAdapter(BasePlatformAdapter):
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
            return (
                event_or_target.get("platform") == "gcp"
                or "gcp" in str(event_or_target.get("namespace", "")).lower()
            )
        return False

    def detect_target(self, events: list[dict[str, Any]]) -> TargetResource:
        evt = events[0] if events else {}
        res = str(
            evt.get(
                "resource_name",
                "projects/prod-cluster/zones/us-central1/instances/api-gw",
            )
        )
        name = res.split("/")[-1]
        zone = res.split("/")[3] if len(res.split("/")) > 3 else "us-central1"
        return TargetResource(
            platform="gcp",
            namespace=zone,
            name=name,
            kind="compute_instance",
            arn_or_uri=res,
        )

    async def collect_telemetry(self, target: TargetResource) -> PlatformTelemetry:
        return PlatformTelemetry(
            target=target,
            metrics={"cpu_utilization": 0.99},
            recent_logs=["GCP Compute VM memory pressure detected"],
            health_status="failing",
        )

    async def inspect_live_state(self, target: TargetResource) -> LiveStateSnapshot:
        return LiveStateSnapshot(target=target, exists=True, is_failing=True)

    def get_tools(self) -> list[NexusTool]:
        return list(self._tools.values())

    async def execute_action(
        self, tool_name: str, parameters: dict[str, Any]
    ) -> NexusToolResult:
        self.restarted = True
        return await self._tools[tool_name].execute(**parameters)

    async def capture_snapshot(
        self, target: TargetResource, tool_name: str, parameters: dict[str, Any]
    ) -> RollbackSnapshot | None:
        return RollbackSnapshot(
            snapshot_id="gcp-snap-123",
            target=target,
            action_name=tool_name,
            rollback_tool=tool_name,
            rollback_parameters=parameters,
            description="Revert GCP restart",
        )

    async def execute_rollback(self, snapshot: RollbackSnapshot) -> NexusToolResult:
        return NexusToolResult(success=True, data={"rollback": "gcp_reverted"})

    async def verify_health(
        self, target: TargetResource, plan_step: dict[str, Any]
    ) -> HealthCheckResult:
        return HealthCheckResult(
            healthy=True, slo_restored=True, details="Instance RUNNING"
        )


@pytest.mark.asyncio
async def test_future_platform_registration_and_workflow_execution():
    registry = get_platform_registry()
    registry.register(MockGCPPlatformAdapter())

    event = {
        "platform": "gcp",
        "resource_name": "projects/prod-proj/zones/us-central1-a/instances/payment-worker-v1",
        "severity": "critical",
        "signal_type": "gcp_vm_unresponsive",
    }
    assert registry.resolve_adapter(event).platform_id == "gcp"

    workflow = IncidentWorkflow()
    with patch("nexus.graph.nodes.diagnose.get_llm", return_value=MockFutureGCPModel()):
        result = await workflow.run_incident(
            {
                "incident_id": "inc-gcp-future-001",
                "platform": "gcp",
                "events": [event],
                "approval_decision": "approved",
            }
        )

    assert result["resolved"] is True
    assert result["fsm_state"] == "resolved"
    stages = [entry["stage"] for entry in result["audit_log"]]
    for s in [
        "triage",
        "collect_telemetry",
        "diagnose",
        "plan",
        "govern",
        "execute",
        "verify",
        "learn",
    ]:
        assert s in stages
