"""
Unit tests for NEXUS LangGraph Approval Node, Interrupt, and HITL Edit Routing.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from nexus.engine.fsm import IncidentState
from nexus.graph.nodes.approval import approval_node
from nexus.graph.platform import HealthCheckResult, get_platform_registry
from nexus.graph.routes import route_after_approval
from nexus.graph.workflow import IncidentWorkflow
from nexus.integration.slack_templates import build_agentic_approval_payload
from nexus.tools.base import NexusToolResult


class MockRCAChatModel(BaseChatModel):
    """Mock LLM that immediately emits structured RCAReport tool call."""

    @property
    def _llm_type(self) -> str:
        return "mock-rca-chat-model"

    def _generate(self, *args: Any, **kwargs: Any) -> ChatResult:
        msg = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "RCAReport",
                    "args": {
                        "root_cause_summary": "OOMKilled pod due to memory limit exhaustion",
                        "affected_resource_urn": "k8s://production/apps/v1/Deployment/checkout-api",
                        "provider": "kubernetes",
                        "confidence": 0.95,
                        "hypotheses_evaluated": ["oom", "bad_deploy"],
                        "evidence_chain": [
                            {
                                "tool_name": "execute_k8s_get_logs",
                                "tool_arguments": {
                                    "pod_name": "checkout-api-123",
                                    "namespace": "production",
                                },
                                "key_finding": "Pod status terminated with exitCode 137",
                            }
                        ],
                        "pre_incident_resource_state": {"replicas": 2},
                        "requires_remediation": True,
                    },
                    "id": "rca_call_1",
                }
            ],
        )
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def bind_tools(self, *args: Any, **kwargs: Any) -> Any:
        return self


def test_route_after_approval_logic():
    """Verify routing decisions based on operator verdicts."""
    assert route_after_approval({"approval_decision": "approved"}) == "execute"
    assert route_after_approval({"approval_decision": "yes"}) == "execute"
    assert route_after_approval({"approval_decision": "proceed"}) == "execute"

    assert route_after_approval({"approval_decision": "edit"}) == "govern"
    assert route_after_approval({"approval_decision": "modify"}) == "govern"

    assert route_after_approval({"approval_decision": "rejected"}) == "escalate"
    assert route_after_approval({"approval_decision": "no"}) == "escalate"
    assert route_after_approval({"approval_decision": "unknown"}) == "escalate"


def test_approval_node_processes_verdicts_and_edits():
    """Verify approval_node correctly parses approved, edit, and rejected payloads."""
    # 1. Approved string
    state_approved = {
        "incident_id": "inc-app-01",
        "approval_decision": "approved",
    }
    res1 = approval_node(state_approved)
    assert res1["approval_decision"] == "approved"
    assert res1["fsm_state"] == IncidentState.EXECUTING.value

    # 2. Rejected string
    state_rejected = {
        "incident_id": "inc-app-02",
        "approval_decision": "rejected",
    }
    res2 = approval_node(state_rejected)
    assert res2["approval_decision"] == "rejected"
    assert res2["fsm_state"] == IncidentState.REJECTED.value

    # 3. Edit dict with updated parameters
    state_edit = {
        "incident_id": "inc-app-03",
        "approval_decision": {
            "action": "edit",
            "parameters": {"replicas": 5, "namespace": "production"},
        },
        "plan": {
            "steps": [
                {
                    "step_index": 0,
                    "tool_name": "k8s_scale_resource",
                    "parameters": {"replicas": 2},
                }
            ]
        },
    }
    res3 = approval_node(state_edit)
    assert res3["approval_decision"] == "edit"
    assert res3["fsm_state"] == IncidentState.POLICY_CHECK.value
    assert res3["plan"]["steps"][0]["parameters"]["replicas"] == 5


def test_slack_block_kit_agentic_approval_card():
    """Verify build_agentic_approval_payload constructs rich Block Kit approval cards."""
    proposed_steps = [
        {
            "step_index": 0,
            "tool_name": "k8s_scale_resource",
            "blast_radius": "service",
            "is_destructive": False,
            "parameters": {"deployment_name": "checkout-web", "replicas": 5},
            "rollback_tool": "k8s_scale_resource",
            "rollback_parameters": {"replicas": 2},
        },
        {
            "step_index": 1,
            "tool_name": "aws_rds_delete_db_instance",
            "blast_radius": "database",
            "is_destructive": True,
            "parameters": {"db_instance_identifier": "legacy-db"},
        },
    ]

    card = build_agentic_approval_payload(
        incident_id="inc-test-card",
        target_service="checkout-web",
        causal_rationale="Traffic surge caused pod crash; scaling recommended.",
        proposed_steps=proposed_steps,
        confidence=0.92,
        platform="kubernetes",
    )

    attachments = card.get("attachments", [])
    assert len(attachments) == 1
    blocks = attachments[0].get("blocks", [])
    assert len(blocks) >= 5

    # Check header
    header_block = blocks[0]
    assert "checkout-web" in header_block["text"]["text"]

    # Check action buttons
    action_block = blocks[-1]
    assert action_block["type"] == "actions"
    actions = [btn["action_id"] for btn in action_block["elements"]]
    assert "nexus_approve" in actions
    assert "nexus_edit" in actions
    assert "nexus_reject" in actions


@pytest.mark.asyncio
async def test_e2e_approval_interrupt_and_resume_approved():
    """Test full workflow pausing on approval and resuming cleanly upon approval."""
    workflow = IncidentWorkflow()
    thread_id = "thread-hitl-approve"
    adapter = get_platform_registry().get("kubernetes")

    events = [
        {
            "agent": "k8s",
            "resource_name": "checkout-api",
            "namespace": "production",
            "severity": "critical",
            "signal_type": "pod_oomkilled",
        }
    ]

    with (
        patch(
            "nexus.graph.nodes.diagnose.get_llm",
            return_value=MockRCAChatModel(),
        ),
        patch.object(
            adapter,
            "execute_action",
            AsyncMock(return_value=NexusToolResult(success=True)),
        ),
        patch.object(
            adapter,
            "verify_health",
            AsyncMock(
                return_value=HealthCheckResult(
                    healthy=True, slo_restored=True, details="Pods Ready"
                )
            ),
        ),
    ):
        interrupted = await workflow.run_incident(
            {
                "incident_id": "inc-hitl-01",
                "platform": "kubernetes",
                "events": events,
            },
            thread_id=thread_id,
        )

        assert interrupted["fsm_state"] == "approval_pending"
        assert len(interrupted["execution_records"]) == 0

        # Resume with approved
        resumed = await workflow.resume_incident(
            thread_id=thread_id,
            approval_decision="approved",
        )

        assert resumed["resolved"] is True
        assert resumed["fsm_state"] == "resolved"
        assert len(resumed["execution_records"]) == 1
        assert resumed["execution_records"][0]["success"] is True


@pytest.mark.asyncio
async def test_e2e_approval_interrupt_and_resume_rejected():
    """Test full workflow pausing on approval and escalating when operator rejects."""
    workflow = IncidentWorkflow()
    thread_id = "thread-hitl-reject"

    events = [
        {
            "agent": "k8s",
            "resource_name": "checkout-api",
            "namespace": "production",
            "severity": "critical",
            "signal_type": "pod_oomkilled",
        }
    ]

    with patch(
        "nexus.graph.nodes.diagnose.get_llm",
        return_value=MockRCAChatModel(),
    ):
        interrupted = await workflow.run_incident(
            {
                "incident_id": "inc-hitl-02",
                "platform": "kubernetes",
                "events": events,
            },
            thread_id=thread_id,
        )

        assert interrupted["fsm_state"] == "approval_pending"

        # Resume with rejected
        resumed = await workflow.resume_incident(
            thread_id=thread_id,
            approval_decision="rejected",
        )

        assert resumed["resolved"] is False
        assert resumed["escalated"] is True
        assert resumed["fsm_state"] == "escalated"


@pytest.mark.asyncio
async def test_e2e_approval_interrupt_resume_edit_re_governs_and_executes():
    """Test operator editing proposed arguments: loops back to govern and executes edited plan."""
    workflow = IncidentWorkflow()
    thread_id = "thread-hitl-edit"
    adapter = get_platform_registry().get("kubernetes")

    events = [
        {
            "agent": "k8s",
            "resource_name": "checkout-api",
            "namespace": "production",
            "severity": "warning",
            "signal_type": "high_cpu_throttle",
        }
    ]

    with (
        patch(
            "nexus.graph.nodes.diagnose.get_llm",
            return_value=MockRCAChatModel(),
        ),
        patch.object(
            adapter,
            "execute_action",
            AsyncMock(return_value=NexusToolResult(success=True)),
        ),
        patch.object(
            adapter,
            "verify_health",
            AsyncMock(
                return_value=HealthCheckResult(
                    healthy=True, slo_restored=True, details="Healthy after edit"
                )
            ),
        ),
    ):
        interrupted = await workflow.run_incident(
            {
                "incident_id": "inc-hitl-03",
                "platform": "kubernetes",
                "events": events,
            },
            thread_id=thread_id,
        )

        assert interrupted["fsm_state"] == "approval_pending"

        # Operator edits plan: changes parameters
        resumed = await workflow.resume_incident(
            thread_id=thread_id,
            approval_decision={
                "action": "edit",
                "parameters": {
                    "namespace": "production",
                    "deployment_name": "checkout-api",
                    "custom_annotation": "operator-modified",
                },
            },
        )

        assert resumed["resolved"] is True
        assert resumed["fsm_state"] == "resolved"
        assert len(resumed["execution_records"]) == 1
        executed_params = resumed["plan"]["steps"][0]["parameters"]
        assert executed_params.get("custom_annotation") == "operator-modified"


@pytest.mark.asyncio
async def test_e2e_approval_interrupt_resume_edit_violating_policy_escalates():
    """Test operator editing plan to a destructive tool: govern blocks it and escalates."""
    workflow = IncidentWorkflow()
    thread_id = "thread-hitl-edit-unsafe"

    events = [
        {
            "agent": "k8s",
            "resource_name": "checkout-api",
            "namespace": "production",
            "severity": "warning",
            "signal_type": "high_cpu_throttle",
        }
    ]

    with patch(
        "nexus.graph.nodes.diagnose.get_llm",
        return_value=MockRCAChatModel(),
    ):
        interrupted = await workflow.run_incident(
            {
                "incident_id": "inc-hitl-04",
                "platform": "kubernetes",
                "events": events,
            },
            thread_id=thread_id,
        )

        assert interrupted["fsm_state"] == "approval_pending"

        # Operator edits plan to insert a destructive tool
        resumed = await workflow.resume_incident(
            thread_id=thread_id,
            approval_decision={
                "action": "edit",
                "steps": [
                    {
                        "step_index": 0,
                        "tool_name": "aws_rds_delete_db_instance",
                        "parameters": {"db_instance_identifier": "production-db"},
                    }
                ],
            },
        )

        # Zero-trust govern node intercepts the destructive tool and routes to escalate
        assert resumed["resolved"] is False
        assert resumed["escalated"] is True
        assert resumed["fsm_state"] == "escalated"
        assert len(resumed["execution_records"]) == 0
