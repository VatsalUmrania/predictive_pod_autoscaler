from unittest.mock import AsyncMock

import pytest

from tests.unit.nexus.test_db_helpers import MockPostgresClient
from nexus.engine.fsm import IncidentFSM, IncidentState


@pytest.mark.asyncio
async def test_fsm_happy_path_with_db_and_nats():
    db = MockPostgresClient()
    await db.initialize()
    inc_id = await db.create_incident(
        fingerprint="fp-fsm-1",
        environment="kubernetes",
        target_resource="default/checkout-service",
    )

    mock_nats = AsyncMock()
    mock_nats.publish_raw = AsyncMock()

    fsm = IncidentFSM(incident_id=inc_id, db_client=db, nats_client=mock_nats)
    assert fsm.current_state == IncidentState.DETECTED

    # 1. Correlate
    ok = await fsm.transition_to(IncidentState.CORRELATED, reason="Clustered 3 error signals")
    assert ok is True
    assert fsm.current_state == IncidentState.CORRELATED
    assert mock_nats.publish_raw.called

    # 2. Diagnosing
    ok = await fsm.transition_to(IncidentState.DIAGNOSING, reason="Starting Gemini RCA")
    assert ok is True

    # 3. Planning
    ok = await fsm.transition_to(IncidentState.PLANNING, reason="RCA complete, selecting runbook")
    assert ok is True

    # 4. Policy Check
    ok = await fsm.transition_to(IncidentState.POLICY_CHECK, reason="Evaluating OPA policies")
    assert ok is True

    # 5. Executing
    ok = await fsm.transition_to(IncidentState.EXECUTING, reason="Approved L1 automated action")
    assert ok is True

    # 6. Verifying
    ok = await fsm.transition_to(IncidentState.VERIFYING, reason="Awaiting post-restart metrics")
    assert ok is True

    # 7. Resolved
    ok = await fsm.transition_to(IncidentState.RESOLVED, reason="Error rate returned to 0%")
    assert ok is True
    assert fsm.is_terminal() is True

    # Verify DB transition history
    trace = await db.get_incident_trace(inc_id)
    assert trace["incident"]["current_state"] == "resolved"
    transitions = [t["to_state"] for t in trace["state_transitions"]]
    assert transitions == [
        "detected", "correlated", "diagnosing", "planning",
        "policy_check", "executing", "verifying", "resolved"
    ]

    await db.close()


@pytest.mark.asyncio
async def test_fsm_invalid_transition_rejected():
    db = MockPostgresClient()
    await db.initialize()
    inc_id = await db.create_incident(
        fingerprint="fp-fsm-2",
        environment="aws",
        target_resource="arn:aws:lambda:us-east-1:123456789:function:order-processor",
    )

    fsm = IncidentFSM(incident_id=inc_id, db_client=db)
    # Skipping straight from DETECTED to EXECUTING is forbidden
    ok = await fsm.transition_to(IncidentState.EXECUTING, reason="Illegal shortcut")
    assert ok is False
    assert fsm.current_state == IncidentState.DETECTED

    inc = await db.get_incident(inc_id)
    assert inc["current_state"] == "detected"
    await db.close()


@pytest.mark.asyncio
async def test_fsm_rollback_and_escalation():
    db = MockPostgresClient()
    await db.initialize()
    inc_id = await db.create_incident(
        fingerprint="fp-fsm-3",
        environment="hybrid",
        target_resource="default/payment-api",
    )

    fsm = IncidentFSM(incident_id=inc_id, db_client=db)
    await fsm.transition_to(IncidentState.CORRELATED)
    await fsm.transition_to(IncidentState.DIAGNOSING)
    await fsm.transition_to(IncidentState.POLICY_CHECK)
    await fsm.transition_to(IncidentState.EXECUTING)

    # Action caused error -> trigger rollback
    ok = await fsm.transition_to(IncidentState.ROLLING_BACK, reason="Post-checks degraded")
    assert ok is True

    ok = await fsm.transition_to(IncidentState.ROLLED_BACK, reason="Restored previous ReplicaSet")
    assert ok is True

    # Rollback still requires human attention
    ok = await fsm.transition_to(IncidentState.ESCALATED, reason="Escalated to on-call engineer")
    assert ok is True
    assert fsm.current_state == IncidentState.ESCALATED

    await db.close()
