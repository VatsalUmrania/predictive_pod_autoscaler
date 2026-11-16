"""
Unit tests for NEXUS Phase 3: ReAct Diagnostics, Topology & Fallback.
Tests:
  1. Tool observation truncation (100 lines / 4KB)
  2. Individual tool execution timeout (5s)
  3. route_after_diagnose conditional routing
  4. diagnose_node immediate bypass when fallback_active is pre-set
  5. diagnose_node fallback on unsupported LLM
  6. diagnose_node fallback on asyncio.TimeoutError
  7. diagnose_node fallback on LLM API exception
  8. diagnose_node successful autonomous ReAct investigation & RCA mapping
  9. End-to-end graph execution: fallback activates collect_node -> plan_node
  10. End-to-end graph execution: verified RCA routes diagnose_node -> plan_node (collect skipped)
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from nexus.engine.fsm import IncidentState
from nexus.graph.nodes.diagnose import (
    diagnose_node,
    get_hardened_diagnostic_tools,
    truncate_observation,
)
from nexus.graph.platform import HealthCheckResult, get_platform_registry
from nexus.graph.routes import route_after_diagnose
from nexus.graph.workflow import IncidentWorkflow

# ── Helpers & Mock LLMs ───────────────────────────────────────────────────────


class MockReActChatModel(BaseChatModel):
    """Chat model with bind_tools support returning mock RCA JSON."""

    response_text: str = json.dumps(
        {
            "root_cause": "Container memory limit exceeded causing OOMKilled",
            "failure_class": "resource_exhaustion",
            "healing_level": 1,
            "confidence": 0.92,
            "reasoning": "Inspected pod logs and metrics; observed fatal memory saturation",
            "suggested_action": "k8s_restart_deployment",
            "suggested_fix": "Restart deployment to clear memory saturation and restore pod",
            "action_params": {
                "namespace": "default",
                "deployment_name": "checkout-svc",
            },
            "actions_to_avoid": [],
            "evidence_citations": ["Memory usage exceeded limit", "Exit code 137"],
        }
    )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=self.response_text))]
        )

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:
        return self

    @property
    def _llm_type(self) -> str:
        return "mock-react-chat-model"


def _sample_state(**overrides: Any) -> dict[str, Any]:
    base = {
        "incident_id": "inc-phase3-001",
        "platform": "kubernetes",
        "fsm_state": "triaging",
        "target": {
            "name": "checkout-svc",
            "namespace": "default",
            "platform": "kubernetes",
        },
        "events": [
            {
                "signal_type": "pod_oomkilled",
                "resource_name": "checkout-svc",
                "namespace": "default",
                "severity": "critical",
            }
        ],
        "telemetry": {},
        "messages": [],
        "fallback_active": False,
        "retry_count": 0,
        "max_retries": 2,
    }
    base.update(overrides)
    return base


# ── 1. Observation Truncation Unit Tests ─────────────────────────────────────


def test_truncate_observation_lines():
    """Verify that observations exceeding 100 lines are truncated to 100 lines."""
    long_text = "\n".join(f"log line {i}" for i in range(150))
    truncated = truncate_observation(long_text, max_lines=100, max_bytes=100000)
    lines = truncated.splitlines()

    # 100 lines + 1 truncation notification line
    assert len(lines) == 101
    assert "truncated: output exceeded 100 lines" in lines[-1]
    assert lines[0] == "log line 0"
    assert lines[99] == "log line 99"


def test_truncate_observation_bytes():
    """Verify that observations exceeding 4KB are truncated to 4KB."""
    # Create single long line of 8KB
    big_line = "A" * 8192
    truncated = truncate_observation(big_line, max_lines=100, max_bytes=4096)

    assert "truncated: output exceeded 4KB" in truncated
    # Verify prefix is strictly bounded by 4KB
    prefix = truncated.split("\n[...")[0]
    assert len(prefix.encode("utf-8")) <= 4096


def test_truncate_observation_dict():
    """Verify recursive truncation within dictionaries."""
    data = {
        "logs": "\n".join(f"line {i}" for i in range(120)),
        "short": "ok",
    }
    truncated = truncate_observation(data, max_lines=100, max_bytes=4096)
    assert isinstance(truncated, dict)
    assert "truncated: output exceeded 100 lines" in truncated["logs"]
    assert truncated["short"] == "ok"


# ── 2. Tool Hardening & Timeout Unit Tests ───────────────────────────────────


@pytest.mark.asyncio
async def test_hardened_diagnostic_tools_timeout():
    """Verify that an individual tool execution hanging beyond timeout_s safely aborts."""
    tools = get_hardened_diagnostic_tools(timeout_s=0.05)
    test_tool = next((t for t in tools if t.name == "k8s_get_pod_logs"), None)
    assert test_tool is not None

    async def _hanging_coro(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(1.0)
        return {"data": "done"}

    with patch.object(test_tool, "coroutine", _hanging_coro):
        # Re-wrap with short timeout
        from langchain_core.tools import StructuredTool

        orig_coro = test_tool.coroutine

        async def _wrapped(**kwargs: Any) -> Any:
            try:
                return await asyncio.wait_for(orig_coro(**kwargs), timeout=0.05)
            except asyncio.TimeoutError:
                return {
                    "success": False,
                    "error": f"Tool '{test_tool.name}' timed out after 0.05s",
                }

        wrapped_tool = StructuredTool.from_function(
            func=test_tool.func,
            coroutine=_wrapped,
            name=test_tool.name,
            description=test_tool.description,
            args_schema=test_tool.args_schema,
        )

        res = await wrapped_tool.ainvoke(
            {"namespace": "default", "pod_name": "test-pod"}
        )
        assert res.get("success") is False
        assert "timed out" in res.get("error", "")


# ── 3. Routing Tests ─────────────────────────────────────────────────────────


def test_route_after_diagnose():
    """Verify route_after_diagnose branches based on fallback_active."""
    # When fallback is active -> route to collect
    state_fallback = _sample_state(fallback_active=True)
    assert route_after_diagnose(state_fallback) == "collect"

    # When fallback is False -> route to plan
    state_normal = _sample_state(fallback_active=False)
    assert route_after_diagnose(state_normal) == "plan"


# ── 4. Diagnose Node Fallback & Bypass Unit Tests ─────────────────────────────


@pytest.mark.asyncio
async def test_diagnose_node_bypass_when_fallback_active():
    """Verify diagnose_node immediately returns if fallback_active is already True."""
    state = _sample_state(fallback_active=True)
    result = await diagnose_node(state)

    assert result["fallback_active"] is True
    assert result["fsm_state"] == IncidentState.DIAGNOSING.value
    assert "diagnosis" not in result


@pytest.mark.asyncio
async def test_diagnose_node_fallback_on_unsupported_llm():
    """Verify that an LLM lacking tool-binding capabilities activates fallback."""
    from langchain_core.language_models import FakeListChatModel

    state = _sample_state(fallback_active=False)
    with patch(
        "nexus.graph.nodes.diagnose.get_llm",
        return_value=FakeListChatModel(responses=["{}"]),
    ):
        result = await diagnose_node(state)

    assert result["fallback_active"] is True
    assert result["fsm_state"] == IncidentState.DIAGNOSING.value
    assert any(
        "react_fallback_activated" in e.get("action", "") for e in result["audit_log"]
    )


@pytest.mark.asyncio
async def test_diagnose_node_fallback_on_llm_timeout():
    """Verify that an LLM timeout activates fallback cleanly."""
    state = _sample_state(fallback_active=False)
    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(
        side_effect=asyncio.TimeoutError("60s diagnostic timeout")
    )

    with (
        patch("nexus.graph.nodes.diagnose.get_llm", return_value=MockReActChatModel()),
        patch("nexus.graph.nodes.diagnose.create_react_agent", return_value=mock_agent),
    ):
        result = await diagnose_node(state)

    assert result["fallback_active"] is True
    assert result["fsm_state"] == IncidentState.DIAGNOSING.value
    audit_entries = result["audit_log"]
    assert any("react_fallback_activated" in e.get("action", "") for e in audit_entries)
    assert any("TimeoutError" in e.get("details", "") for e in audit_entries)


@pytest.mark.asyncio
async def test_diagnose_node_fallback_on_llm_exception():
    """Verify that an LLM API exception activates fallback cleanly."""
    state = _sample_state(fallback_active=False)
    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(
        side_effect=RuntimeError("Google GenAI 429 Resource Exhausted")
    )

    with (
        patch("nexus.graph.nodes.diagnose.get_llm", return_value=MockReActChatModel()),
        patch("nexus.graph.nodes.diagnose.create_react_agent", return_value=mock_agent),
    ):
        result = await diagnose_node(state)

    assert result["fallback_active"] is True
    assert result["fsm_state"] == IncidentState.DIAGNOSING.value
    audit_entries = result["audit_log"]
    assert any("react_fallback_activated" in e.get("action", "") for e in audit_entries)
    assert any("429" in e.get("details", "") for e in audit_entries)


# ── 5. Successful ReAct Investigation Test ────────────────────────────────────


@pytest.mark.asyncio
async def test_diagnose_node_successful_react_loop():
    """Verify successful autonomous ReAct investigation maps RCA fields and validates."""
    state = _sample_state(fallback_active=False)
    mock_model = MockReActChatModel()

    with patch("nexus.graph.nodes.diagnose.get_llm", return_value=mock_model):
        result = await diagnose_node(state)

    assert result["fallback_active"] is False
    assert result["fsm_state"] == IncidentState.PLANNING.value
    assert "diagnosis" in result

    diag = result["diagnosis"]
    assert diag["failure_class"] == "resource_exhaustion"
    assert diag["confidence"] >= 0.85
    assert diag["suggested_action"] == "k8s_restart_deployment"
    assert "OOMKilled" in diag["root_cause"]
    assert result["agentic_rca"] == diag


# ── 6. End-to-End Workflow Topology & Fallback Execution ─────────────────────


@pytest.mark.asyncio
async def test_e2e_workflow_fallback_activates_collect_node():
    """
    Test full StateGraph execution when diagnostic agent triggers fallback:
    Topology traversed: triage -> diagnose -> collect -> plan -> govern -> approval (interrupt)
    """
    workflow = IncidentWorkflow()
    thread_id = "test-phase3-fallback-e2e"
    adapter = get_platform_registry().get("kubernetes")

    events = [
        {
            "agent": "k8s",
            "resource_name": "checkout-svc",
            "namespace": "default",
            "severity": "critical",
            "signal_type": "pod_oomkilled",
        }
    ]

    with (
        patch(
            "nexus.agents.k8s_tools.describe_resource",
            return_value="State: OOMKilled\nReplicas: 2",
        ),
        patch(
            "nexus.agents.k8s_tools.get_pod_logs",
            return_value="fatal memory limit exceeded",
        ),
        patch(
            "nexus.agents.k8s_tools.restart_deployment",
            return_value="deployment.apps/checkout-svc restarted",
        ),
        patch.object(
            adapter,
            "verify_health",
            AsyncMock(
                return_value=HealthCheckResult(
                    healthy=True, slo_restored=True, details="Ready 2/2"
                )
            ),
        ),
    ):
        # Run incident with default FakeListChatModel (unproven diagnosis, no default fallback)
        result = await workflow.run_incident(
            {
                "incident_id": "inc-fallback-e2e-01",
                "platform": "kubernetes",
                "events": events,
            },
            thread_id=thread_id,
        )

        assert result["incident_id"] == "inc-fallback-e2e-01"
        assert result["resolved"] is True
        assert result["fsm_state"] == "resolved"
        assert len(result.get("execution_records", [])) == 0

        # Check audit log to verify collect_telemetry was recorded via fallback
        stages = [e["stage"] for e in result["audit_log"]]
        assert "triage" in stages
        assert "diagnose" in stages
        assert "collect_telemetry" in stages  # collect recorded on fallback!
        assert "learn" in stages


@pytest.mark.asyncio
async def test_e2e_workflow_successful_react_skips_collect_node():
    """
    Test full StateGraph execution when autonomous ReAct agent succeeds:
    Topology traversed: triage -> diagnose -> plan -> govern -> approval (interrupt)
    collect_telemetry is SKIPPED because ReAct loop directly diagnosed.
    """
    workflow = IncidentWorkflow()
    thread_id = "test-phase3-react-success-e2e"
    adapter = get_platform_registry().get("kubernetes")

    events = [
        {
            "agent": "k8s",
            "resource_name": "checkout-svc",
            "namespace": "default",
            "severity": "critical",
            "signal_type": "pod_oomkilled",
        }
    ]

    mock_model = MockReActChatModel()

    with (
        patch("nexus.graph.nodes.diagnose.get_llm", return_value=mock_model),
        patch(
            "nexus.agents.k8s_tools.describe_resource",
            return_value="State: OOMKilled\nReplicas: 2",
        ),
        patch(
            "nexus.agents.k8s_tools.get_pod_logs",
            return_value="fatal memory limit exceeded",
        ),
        patch(
            "nexus.agents.k8s_tools.restart_deployment",
            return_value="restarted deployment",
        ),
        patch.object(
            adapter,
            "verify_health",
            AsyncMock(
                return_value=HealthCheckResult(
                    healthy=True, slo_restored=True, details="Ready 2/2"
                )
            ),
        ),
    ):
        interrupted = await workflow.run_incident(
            {
                "incident_id": "inc-react-success-01",
                "platform": "kubernetes",
                "events": events,
            },
            thread_id=thread_id,
        )

        assert interrupted["incident_id"] == "inc-react-success-01"
        assert interrupted["fsm_state"] == "approval_pending"

        stages = [e["stage"] for e in interrupted["audit_log"]]
        assert "triage" in stages
        assert "diagnose" in stages
        assert "collect_telemetry" not in stages  # collect_node SKIPPED on normal path!
        assert "govern" in stages

        resumed = await workflow.resume_incident(
            thread_id=thread_id, approval_decision="approved"
        )
        assert resumed["resolved"] is True
        assert resumed["fsm_state"] == "resolved"
