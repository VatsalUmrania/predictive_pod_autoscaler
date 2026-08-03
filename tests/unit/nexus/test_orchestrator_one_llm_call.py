"""One LLM call per incident.

After the fix, ``_process_cluster`` makes a single LLM request (the RCA) and
stages the remediation deterministically — it no longer routes a gemini/openai
RCA into ``LLMOrchestrator.handle_cluster`` for a SECOND model round-trip.
These tests pin that contract: the RCA is awaited exactly once and one approval
is staged. ``# ponytail: manual-review branch is a second path — covered too.``
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bus.incident_event import (
    AgentType,
    IncidentEvent,
    Severity,
    SignalType,
)
from nexus.governance.action_ladder import HumanApprovalQueue
from nexus.governance.runbook import RunbookLibrary
from nexus.reasoning.incident_cluster import IncidentCluster
from nexus.reasoning.orchestrator import NexusOrchestrator
from nexus.reasoning.rca_engine import RCAResult

_POD_RUNBOOK = """\
runbook:
  id: "runbook_pod_crashloop_v1"
  healing_level: 1
  trigger:
    signal_types: ["pod_crashloop"]
  actions:
    - type: "restart_pod"
"""


def _orchestrator(rca: AsyncMock, tmp_path) -> tuple[NexusOrchestrator, HumanApprovalQueue]:
    tmp_path.joinpath("runbook_pod_crashloop_v1.yaml").write_text(_POD_RUNBOOK)

    nats = MagicMock()
    nats.publish = AsyncMock()
    scorer = MagicMock()
    scorer.score = MagicMock(return_value=0.72)
    scorer.gate = MagicMock(return_value=3)
    scorer.describe = MagicMock(return_value="confident")

    rca_engine = MagicMock()
    rca_engine.analyze = rca

    executor = MagicMock()
    queue = HumanApprovalQueue()
    executor.ladder.approval_queue = queue
    executor.library = RunbookLibrary(tmp_path)

    orc = NexusOrchestrator(
        nats_client=nats,
        correlator=MagicMock(),
        rca_engine=rca_engine,
        confidence_scorer=scorer,
        executor=executor,
    )
    return orc, queue


def _cluster() -> IncidentCluster:
    return IncidentCluster.new(
        IncidentEvent(
            agent=AgentType.K8S,
            signal_type=SignalType.POD_CRASHLOOP,
            severity=Severity.WARNING,
            namespace="default",
            resource_name="payments",
            context={},
            correlation_id="CL-1",
            confidence=0.7,
        )
    )


@pytest.mark.asyncio
async def test_one_llm_call_per_incident_stages_runbook_for_approval(tmp_path):
    """The RCA is the single LLM call; its suggested runbook is staged once."""
    rca_result = RCAResult(
        root_cause="bad image",
        failure_class="bad_deploy",
        healing_level=1,
        runbook_id="runbook_pod_crashloop_v1",
        confidence=0.72,
        reasoning="crashloop",
        source="gemini",
    )
    cluster = _cluster()
    orc, queue = _orchestrator(AsyncMock(return_value=rca_result), tmp_path)

    await orc._process_cluster(cluster)

    # ONE LLM call — analyze awaited exactly once, never the old second handle_cluster.
    orc.rca.analyze.assert_awaited_once_with(cluster)
    # Remediation staged for human approval, not auto-executed.
    assert orc._actions_dispatched == 1
    assert len(queue.pending_list()) == 1
    p = queue.pending_list()[0]
    assert p.runbook_id == "runbook_pod_crashloop_v1"
    assert p.action_type == "restart_pod"
    assert p.context["rca_source"] == "gemini"


@pytest.mark.asyncio
async def test_llm_rca_without_mapped_runbook_stages_manual_review(tmp_path):
    """An LLM RCA with no library runbook surfaces as manual review — still one call."""
    rca_result = RCAResult(
        root_cause="unknown drift",
        failure_class="unknown",
        healing_level=0,
        runbook_id=None,
        confidence=0.4,
        reasoning="no rule",
        source="openai",
    )
    cluster = _cluster()
    orc, queue = _orchestrator(AsyncMock(return_value=rca_result), tmp_path)

    await orc._process_cluster(cluster)

    orc.rca.analyze.assert_awaited_once()
    p = queue.pending_list()[0]
    assert p.runbook_id == "llm_dynamic"
    assert p.action_type == "manual_review"
    assert p.context["llm_diagnosis"] == "unknown drift"
