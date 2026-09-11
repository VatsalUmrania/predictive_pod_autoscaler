import os
from unittest.mock import AsyncMock, patch

import pytest

from nexus.db.postgres import PostgresClient, get_database_client, _ensure_uuid
from tests.unit.nexus.test_db_helpers import MockPostgresClient


@pytest.mark.asyncio
async def test_mock_db_client_lifecycle():
    """Verify that MockPostgresClient correctly tracks incident lifecycle and traces."""
    client = MockPostgresClient()
    await client.initialize()

    # 1. Create incident
    inc_id = await client.create_incident(
        fingerprint="fp-12345",
        environment="kubernetes",
        target_resource="default/payment-api",
        severity="critical",
        trigger_source="prometheus_alert",
        trigger_payload={"cpu": 95, "restarts": 4},
    )
    assert inc_id is not None

    # Fetch incident
    inc = await client.get_incident(inc_id)
    assert inc is not None
    assert inc["fingerprint"] == "fp-12345"
    assert inc["current_state"] == "detected"
    assert inc["trigger_payload"]["cpu"] == 95

    # 2. Transition state
    await client.transition_state(inc_id, "diagnosing", reason="Correlated with high latency")
    inc = await client.get_incident(inc_id)
    assert inc["current_state"] == "diagnosing"

    # 3. Start Agent Run
    run_id = await client.start_agent_run(inc_id, "orchestrator", "gemini-3.1-flash-lite")
    assert run_id is not None

    # 4. Log Message
    msg_id = await client.log_message(
        run_id=run_id,
        sequence_num=1,
        role="assistant",
        content="Diagnosing OOM on payment-api",
        reasoning_content="Pod restarts correlated with memory limit breach.",
    )
    assert msg_id is not None

    # 5. Log Tool Call
    call_id = await client.log_tool_call(
        run_id=run_id,
        tool_name="k8s_get_pod_logs",
        tool_category="k8s_read",
        input_parameters={"namespace": "default", "pod_name": "payment-api-abc"},
        message_id=msg_id,
    )
    assert call_id is not None

    await client.update_tool_call(
        call_id=call_id,
        status="success",
        output_result={"logs": "OutOfMemoryError: Java heap space"},
        duration_ms=45,
    )

    # 6. Create Remediation Action
    action_id = await client.create_remediation_action(
        incident_id=inc_id,
        run_id=run_id,
        action_name="k8s_patch_resource_limits",
        action_level="L2_MUTATING_APPROVAL",
        target_resource="default/payment-api",
        parameters={"memory": "1Gi"},
        rollback_plan={"action": "k8s_patch_resource_limits", "parameters": {"memory": "512Mi"}},
        pre_check_snapshot={"status": "CrashLoopBackOff"},
    )
    assert action_id is not None

    await client.update_remediation_action(
        action_id=action_id,
        outcome="success",
        post_check_snapshot={"status": "Running"},
        policy_check_passed=True,
        human_approved_by="alice",
    )

    # 7. Finish Agent Run & Resolve Incident
    await client.finish_agent_run(run_id, status="completed", prompt_tokens=150, completion_tokens=80, duration_ms=620)
    await client.transition_state(inc_id, "resolved", reason="Action succeeded and pod running")

    # 8. Verify Full Trace
    trace = await client.get_incident_trace(inc_id)
    assert trace["incident"]["current_state"] == "resolved"
    assert len(trace["state_transitions"]) >= 2
    assert len(trace["agent_runs"]) == 1
    assert len(trace["agent_runs"][0]["messages"]) == 1
    assert len(trace["agent_runs"][0]["tool_calls"]) == 1
    assert len(trace["remediation_actions"]) == 1
    assert trace["remediation_actions"][0]["outcome"] == "success"
    assert trace["remediation_actions"][0]["human_approved_by"] == "alice"


def test_ensure_uuid():
    """Verify deterministic UUID generation."""
    u1 = _ensure_uuid(None)
    assert u1 is not None

    raw_uuid_str = "12345678-1234-5678-1234-567812345678"
    u2 = _ensure_uuid(raw_uuid_str)
    assert str(u2) == raw_uuid_str

    # Arbitrary non-UUID string should be hashed deterministically
    u3 = _ensure_uuid("cluster-a/pod-xyz")
    u4 = _ensure_uuid("cluster-a/pod-xyz")
    assert u3 == u4


@pytest.mark.asyncio
async def test_postgres_client_pool_config(monkeypatch):
    """Verify PostgresClient reads pool size from env."""
    monkeypatch.setenv("NEXUS_PG_MIN_POOL", "4")
    monkeypatch.setenv("NEXUS_PG_MAX_POOL", "16")

    client = PostgresClient(dsn="postgresql://user:pass@localhost:5432/nexus")
    assert client.min_pool == 4
    assert client.max_pool == 16


@pytest.mark.asyncio
async def test_postgres_client_requires_dsn():
    """PostgresClient.initialize raises ValueError if no DSN is provided."""
    client = PostgresClient(dsn="")
    with pytest.raises(ValueError, match="No PostgreSQL DSN configured"):
        await client.initialize()


@pytest.mark.asyncio
async def test_postgres_client_retry_and_fail_fast():
    """PostgresClient retries up to max_retries with backoff, then fails fast."""
    client = PostgresClient(dsn="postgresql://user:pass@localhost:5432/nexus")

    with patch("asyncpg.create_pool", side_effect=ConnectionRefusedError("Connection refused")):
        with pytest.raises(RuntimeError, match="Failed to initialize PostgreSQL pool after 3 attempts"):
            await client.initialize(max_retries=3, initial_backoff=0.01)


@pytest.mark.asyncio
async def test_get_database_client_requires_dsn(monkeypatch):
    """get_database_client fails fast when no DSN is set."""
    monkeypatch.delenv("NEXUS_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    import nexus.db.postgres as pg_mod
    monkeypatch.setattr(pg_mod, "_global_db_client", None)

    with pytest.raises(RuntimeError, match="No PostgreSQL DSN configured"):
        await get_database_client()
