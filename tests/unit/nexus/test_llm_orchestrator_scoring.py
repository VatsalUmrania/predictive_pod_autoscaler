"""P1 tests: LLM proposals are staged at the scorer's calibrated confidence,
not the old hard-coded healing_level=2 / confidence=0.8.

Patches the Gemini chat round-trip and asserts what handle_cluster enqueues —
the single observable that gates the rest of the loop (the human sees the real
confidence + blast radius; execute_approved runs the ladder at the real level).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.governance.action_ladder import HumanApprovalQueue
from nexus.governance.policy_engine import LLM_TOOL_LEVEL
from nexus.reasoning.llm_orchestrator import LLMOrchestrator


def _orchestrator_with_tool_call(tool_name: str, kwargs: dict):
    orc = LLMOrchestrator(model_name="m")
    orc._client = MagicMock()
    # Fake response: one function_call into a write tool.
    # NB: MagicMock(name=...) sets the repr, NOT a .name attribute — set
    # name/args as explicit attributes or .name resolves to a child mock.
    fc = MagicMock()
    fc.name = tool_name
    fc.args = kwargs
    resp = MagicMock()
    resp.function_calls = [fc]
    resp.text = "diagnosis prose"

    chat = MagicMock()
    chat.send_message = AsyncMock(return_value=resp)
    orc._client.aio.chats.create = MagicMock(return_value=chat)
    orc._ensure_client = MagicMock(return_value=True)
    return orc


def _summary():
    return {
        "cluster_id": "CL-X",
        "namespace": "default",
        "primary_resource": "orders",
    }


@pytest.mark.asyncio
async def test_handle_cluster_stages_scored_confidence_not_hardcoded():
    """A cluster scored at 0.62 must stage at 0.62, not the old 0.8."""
    orc = _orchestrator_with_tool_call(
        "restart_deployment", {"namespace": "default", "deployment_name": "orders"}
    )
    queue = HumanApprovalQueue()

    await orc.handle_cluster(_summary(), queue, confidence=0.62, effective_level=1)

    assert len(queue.pending_list()) == 1
    p = queue.pending_list()[0]
    assert p.confidence == 0.62
    # restart_deployment is L1 in LLM_TOOL_LEVEL
    assert p.healing_level == LLM_TOOL_LEVEL["restart_deployment"][0]
    assert p.context["tool_level"] == LLM_TOOL_LEVEL["restart_deployment"][0]
    assert p.context["blast_radius"] == LLM_TOOL_LEVEL["restart_deployment"][1]
    assert p.context["effective_level"] == 1


@pytest.mark.asyncio
async def test_handle_cluster_cordon_stages_at_l3_blast():
    """A cluster-wide tool surfaces at its real L3 / cluster_wide, not capped."""
    orc = _orchestrator_with_tool_call(
        "cordon_node", {"node_name": "worker-3"}
    )
    queue = HumanApprovalQueue()

    # Even if the cluster scored modestly, the proposal's honest blast is L3.
    await orc.handle_cluster(_summary(), queue, confidence=0.55, effective_level=1)

    p = queue.pending_list()[0]
    assert p.healing_level == 3
    assert p.context["blast_radius"] == "cluster_wide"
    assert p.confidence == 0.55  # the human sees the honest low score


@pytest.mark.asyncio
async def test_handle_cluster_readtool_not_staged():
    """A read-tool call (investigation) must not be staged as a remediation."""
    orc = _orchestrator_with_tool_call(
        "get_pod_logs", {"namespace": "default", "pod_name": "x"}
    )
    queue = HumanApprovalQueue()

    await orc.handle_cluster(_summary(), queue, confidence=0.9, effective_level=2)

    assert queue.pending_list() == []
