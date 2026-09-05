"""Unit test verifying fixes for false incident detection, NATS replay, and approval suppression."""

import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from nats.js.api import DeliverPolicy

from nexus.bus.incident_event import AgentType, IncidentEvent, Severity, SignalType
from nexus.bus.nats_client import NATSClient
from nexus.engine.fsm import IncidentFSM, IncidentState
from nexus.governance.action_ladder import HumanApprovalQueue
from nexus.reasoning.event_correlator import EventCorrelator
from nexus.reasoning.incident_cluster import IncidentCluster
from nexus.reasoning.orchestrator import NexusOrchestrator
from nexus.reasoning.rca_engine import RCAResult
from nexus.reasoning.rca_validator import ValidationVerdict


@pytest.mark.asyncio
async def test_nats_client_default_deliver_policy_new():
    """Verify NATSClient.subscribe and subscribe_raw default to DeliverPolicy.NEW."""
    client = NATSClient()
    mock_js = MagicMock()
    mock_sub = MagicMock()
    mock_sub.messages = AsyncMock()
    mock_js.subscribe = AsyncMock(return_value=mock_sub)
    client._js = mock_js

    # Test subscribe
    await client.subscribe(handler=AsyncMock(), agent_filter="k8s")
    assert mock_js.subscribe.await_count == 1
    call_kwargs = mock_js.subscribe.await_args.kwargs
    assert call_kwargs.get("deliver_policy") == DeliverPolicy.NEW

    # Test subscribe_raw
    await client.subscribe_raw("nexus.approvals.>", handler=AsyncMock())
    assert mock_js.subscribe.await_count == 2
    raw_call_kwargs = mock_js.subscribe.await_args.kwargs
    assert raw_call_kwargs.get("deliver_policy") == DeliverPolicy.NEW


@pytest.mark.asyncio
async def test_orchestrator_drops_stale_events():
    """Verify Orchestrator._on_event drops events older than 120s."""
    mock_nats = MagicMock()
    mock_correlator = MagicMock()
    orc = NexusOrchestrator(
        nats_client=mock_nats,
        correlator=mock_correlator,
        rca_engine=MagicMock(),
        confidence_scorer=MagicMock(score=MagicMock(return_value=0.0), gate=MagicMock(return_value=0)),
        executor=MagicMock(),
    )

    # Stale event: 5 hours ago
    stale_event = IncidentEvent(
        agent=AgentType.K8S,
        signal_type=SignalType.POD_CRASHLOOP,
        severity=Severity.WARNING,
        namespace="default",
        resource_name="shop-demo",
        timestamp=datetime.now(timezone.utc) - timedelta(hours=5),
    )

    await orc._on_event(stale_event)
    # Correlator should NOT be called
    mock_correlator.ingest.assert_not_called()

    # Fresh event: 5 seconds ago
    fresh_event = IncidentEvent(
        agent=AgentType.K8S,
        signal_type=SignalType.POD_CRASHLOOP,
        severity=Severity.WARNING,
        namespace="default",
        resource_name="shop-demo",
        timestamp=datetime.now(timezone.utc) - timedelta(seconds=5),
    )
    mock_correlator.ingest.return_value = None

    await orc._on_event(fresh_event)
    mock_correlator.ingest.assert_called_once_with(fresh_event)


@pytest.mark.asyncio
async def test_blocked_rca_validation_never_queues_approval():
    """Verify that when RCAValidator blocks a hallucination, no approval is queued."""
    queue = HumanApprovalQueue()
    mock_executor = MagicMock()
    mock_executor.ladder.approval_queue = queue

    mock_rca = MagicMock()
    hallucinated_rca = RCAResult(
        root_cause="Out of memory",
        failure_class="resource_exhaustion",
        healing_level=1,
        runbook_id="runbook_pod_crashloop_v1",
        confidence=0.75,
        reasoning="hallucination",
        source="gemini",
    )
    mock_rca.analyze = AsyncMock(return_value=hallucinated_rca)

    mock_validator = MagicMock()
    mock_validator.validate.return_value = ValidationVerdict(
        passed=False,
        block_reason="resource_exhaustion requires pod_oomkilled",
        confidence_delta=-0.4,
        consistency_note="BLOCKED",
    )

    orc = NexusOrchestrator(
        nats_client=MagicMock(),
        correlator=MagicMock(),
        rca_engine=mock_rca,
        confidence_scorer=MagicMock(score=MagicMock(return_value=0.0), gate=MagicMock(return_value=0)),
        executor=mock_executor,
        rca_validator=mock_validator,
    )

    event = IncidentEvent(
        agent=AgentType.K8S,
        signal_type=SignalType.DEPLOYMENT_DEGRADED,
        severity=Severity.CRITICAL,
        namespace="monitoring",
        resource_name="prometheus-grafana",
    )
    cluster = IncidentCluster.new(event)

    await orc._process_cluster(cluster)

    # Queue must be completely empty!
    assert len(queue.pending_list()) == 0
    assert orc._actions_dispatched == 0


@pytest.mark.asyncio
async def test_notify_incident_rejected_clears_active_incident():
    """Verify notify_incident_rejected clears _active_incidents and transitions FSM to REJECTED."""
    mock_nats = MagicMock()
    mock_nats.publish_raw = AsyncMock()

    orc = NexusOrchestrator(
        nats_client=mock_nats,
        correlator=MagicMock(),
        rca_engine=MagicMock(),
        confidence_scorer=MagicMock(score=MagicMock(return_value=0.0), gate=MagicMock(return_value=0)),
        executor=MagicMock(),
    )

    fsm = IncidentFSM(incident_id="inc-123", current_state=IncidentState.APPROVAL_PENDING)
    orc._active_incidents["default/shop-demo"] = {
        "incident_id": "inc-123",
        "fsm": fsm,
        "target": "default/shop-demo",
    }

    assert "default/shop-demo" in orc._active_incidents
    assert fsm.current_state == IncidentState.APPROVAL_PENDING

    await orc.notify_incident_rejected("default/shop-demo", "inc-123", reason="Manual operator reject")

    # Target must be removed from active incidents
    assert "default/shop-demo" not in orc._active_incidents
    # FSM must be in REJECTED state
    assert fsm.current_state == IncidentState.REJECTED
