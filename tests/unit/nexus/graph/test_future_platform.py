"""
Tests demonstrating zero-hardcoding extensibility for FUTURE PLATFORMS (e.g. GCP, Azure).
Registering a new platform tool in ToolRegistry enables full ReAct diagnosis,
schema-bound planning, governance, execution, and verification with zero platform-specific
hardcoding in the graph.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from nexus.engine.reasoning.rca.result import (
    DiscoveredTarget,
    RCAResult,
    VerificationResult,
)
from nexus.engine.tools.registry import ToolMetadata, get_tool_registry
from nexus.engine.workflow.workflow import IncidentWorkflow


@pytest.mark.asyncio
async def test_future_platform_registration_and_workflow_execution():
    executed_calls: list[dict[str, Any]] = []

    @tool
    def gcp_restart_compute_instance(
        project_id: str, zone: str, instance_name: str
    ) -> dict[str, Any]:
        """Restart a Google Cloud Compute Engine VM instance."""
        executed_calls.append(
            {"project_id": project_id, "zone": zone, "instance_name": instance_name}
        )
        return {"status": "TERMINATED -> PROVISIONING -> RUNNING"}

    registry = get_tool_registry()
    registry.register_langchain_tool(
        gcp_restart_compute_instance,
        mode="mutate",
        platform="gcp",
        metadata=ToolMetadata(
            blast_radius="pod",
            reversible=True,
            is_destructive=False,
            platform="gcp",
            mode="mutate",
        ),
    )

    class _GCPFakeLLM:
        async def ainvoke(self, messages: Any) -> AIMessage:
            return AIMessage(content="Observed GCP state.", tool_calls=[])

        def bind_tools(self, tools: list[Any]) -> Any:
            names = {t.name for t in tools}

            class _Bound:
                async def ainvoke(self, messages: Any) -> AIMessage:
                    if "gcp_restart_compute_instance" in names:
                        return AIMessage(
                            content="Restarting GCP VM payment-worker-v1.",
                            tool_calls=[
                                {
                                    "id": "call_gcp_1",
                                    "name": "gcp_restart_compute_instance",
                                    "args": {
                                        "project_id": "prod-proj",
                                        "zone": "us-central1-a",
                                        "instance_name": "payment-worker-v1",
                                    },
                                }
                            ],
                        )
                    return AIMessage(content="Observed.", tool_calls=[])

            return _Bound()

        def with_structured_output(self, schema: Any) -> Any:
            class _Struct:
                async def ainvoke(self, messages: Any) -> Any:
                    if schema is RCAResult:
                        return RCAResult(
                            target=DiscoveredTarget(
                                platform="gcp",
                                namespace_or_region="us-central1-a",
                                resource_kind="ComputeInstance",
                                resource_name="payment-worker-v1",
                            ),
                            failure_type="gcp_vm_unresponsive",
                            root_cause_summary="GCP Compute VM memory pressure detected.",
                            confidence=0.95,
                            evidence=["serial console OOM"],
                            can_auto_remediate=True,
                            recommended_strategy="Restart GCE instance payment-worker-v1.",
                        )
                    if schema is VerificationResult:
                        return VerificationResult(
                            verification_passed=True,
                            observed_state_summary="GCE instance payment-worker-v1 RUNNING.",
                        )
                    raise ValueError(schema)

            return _Struct()

    workflow = IncidentWorkflow()
    llm = _GCPFakeLLM()
    try:
        with (
            patch("nexus.engine.nodes.diagnose.get_chat_model", return_value=llm),
            patch("nexus.engine.nodes.plan.get_chat_model", return_value=llm),
            patch("nexus.engine.nodes.verify.get_chat_model", return_value=llm),
            patch(
                "nexus.engine.nodes.govern.evaluate_opa_policy",
                AsyncMock(
                    return_value={
                        "allowed": True,
                        "requires_approval": False,
                        "reason": "Auto-approved",
                    }
                ),
            ),
            patch(
                "nexus.services.observability.dashboard.record_incident_resolution",
                AsyncMock(),
            ),
            patch(
                "nexus.services.reporting.incident_report.publish_post_incident_report",
                AsyncMock(),
            ),
        ):
            result = await workflow.run_incident(
                {
                    "incident_id": "inc-gcp-future-001",
                    "platform": "gcp",
                    "raw_events": [
                        {
                            "platform": "gcp",
                            "resource_name": "payment-worker-v1",
                            "severity": "critical",
                        }
                    ],
                }
            )
    finally:
        registry.unregister("gcp_restart_compute_instance")

    assert result["status"] == "RESOLVED"
    assert result["platform"] == "gcp"
    assert (
        result["target_resource"] == "us-central1-a/ComputeInstance/payment-worker-v1"
    )
    assert len(executed_calls) == 1
    assert executed_calls[0]["instance_name"] == "payment-worker-v1"
