"""Unit tests for nexus.governance.audit_trail.AuditTrail.

Covers the write and query paths:
  - update_outcome()
  - record_approval()
  - record_rejection()
  - query_by_incident()
  - query_by_runbook()
  - query_recent()
"""

import json
import pytest

from nexus.governance.audit_trail import AuditTrail
from tests.unit.nexus.test_db_helpers import MockPostgresClient


@pytest.mark.asyncio
async def test_update_outcome_updates_pending_record():
    """update_outcome() must mutate an existing 'pending' row in place."""
    mock_client = MockPostgresClient()
    audit = AuditTrail(db_client=mock_client)
    await audit.initialize()
    try:
        action_id = await audit.write_pending(
            triggered_by="orchestrator",
            runbook_id="runbook_pod_crashloop_v1",
            healing_level=1,
            target="default/payments-api",
            incident_id="INC-1",
        )

        await audit.update_outcome(
            action_id,
            execution_outcome="success",
            post_check_results={"healthy": True},
            rollback_triggered=False,
            action_results=[{"pod": "payments-api-xyz", "action": "restart"}],
        )

        rows = await audit.query_by_incident("INC-1")
        assert len(rows) == 1
        row = rows[0]
        assert row["action_id"] == action_id
        assert row["execution_outcome"] == "success"
        action_res = json.loads(row["action_results"]) if isinstance(row["action_results"], str) else row["action_results"]
        assert action_res == [{"pod": "payments-api-xyz", "action": "restart"}]
        post_res = json.loads(row["post_check_results"]) if isinstance(row["post_check_results"], str) else row["post_check_results"]
        assert post_res == {"healthy": True}
        assert row["rollback_triggered"] is False
    finally:
        await audit.close()


@pytest.mark.asyncio
async def test_update_outcome_preserves_other_columns():
    """update_outcome() touches outcome columns while preserving runbook_id, level, etc."""
    mock_client = MockPostgresClient()
    audit = AuditTrail(db_client=mock_client)
    await audit.initialize()
    try:
        action_id = await audit.write_pending(
            triggered_by="orchestrator",
            runbook_id="runbook_dns_resolution_failure_v1",
            healing_level=2,
            target="default/api",
            incident_id="INC-2",
        )
        await audit.update_outcome(
            action_id, execution_outcome="rolled_back", rollback_triggered=True
        )
        rows = await audit.query_by_incident("INC-2")
        assert rows[0]["runbook_id"] == "runbook_dns_resolution_failure_v1"
        assert rows[0]["healing_level"] == 2
        assert rows[0]["execution_outcome"] == "rolled_back"
        assert rows[0]["rollback_triggered"] is True
    finally:
        await audit.close()


@pytest.mark.asyncio
async def test_record_approval_persists_approved_row():
    """record_approval() writes an 'approved' audit record attributed to the user."""
    mock_client = MockPostgresClient()
    audit = AuditTrail(db_client=mock_client)
    await audit.initialize()
    try:
        returned = await audit.record_approval("APPROVAL-9F", "api_user")
        assert returned is not None

        rows = await audit.query_recent(limit=10)
        assert len(rows) == 1
        row = rows[0]
        assert row["execution_outcome"] == "approved"
        assert row["runbook_id"] == "system_approval"
        assert row["triggered_by"] == "human:api_user"
        assert row["target"] == "APPROVAL-9F"
    finally:
        await audit.close()


@pytest.mark.asyncio
async def test_record_rejection_persists_rejected_row():
    """record_rejection() writes a 'rejected' audit record."""
    mock_client = MockPostgresClient()
    audit = AuditTrail(db_client=mock_client)
    await audit.initialize()
    try:
        returned = await audit.record_rejection("APPROVAL-1A", "sre-oncall")
        assert returned is not None

        rows = await audit.query_recent(limit=10)
        assert len(rows) == 1
        row = rows[0]
        assert row["execution_outcome"] == "rejected"
        assert row["runbook_id"] == "system_rejection"
        assert row["triggered_by"] == "human:sre-oncall"
        assert row["target"] == "APPROVAL-1A"
    finally:
        await audit.close()


@pytest.mark.asyncio
async def test_record_rules_separately_queryable_by_runbook():
    """Both record_* rows land in the audit table and are queryable by runbook_id."""
    mock_client = MockPostgresClient()
    audit = AuditTrail(db_client=mock_client)
    await audit.initialize()
    try:
        await audit.record_approval("A1", "u")
        await audit.record_rejection("A2", "u")

        approved = await audit.query_by_runbook("system_approval")
        rejected = await audit.query_by_runbook("system_rejection")
        assert len(approved) == 1 and approved[0]["execution_outcome"] == "approved"
        assert len(rejected) == 1 and rejected[0]["execution_outcome"] == "rejected"
    finally:
        await audit.close()


@pytest.mark.asyncio
async def test_audit_trail_tail_alias():
    """tail(n) must return the N most recent records."""
    mock_client = MockPostgresClient()
    audit = AuditTrail(db_client=mock_client)
    await audit.initialize()
    try:
        await audit.record_approval("A1", "u1")
        await audit.record_approval("A2", "u2")
        await audit.record_rejection("A3", "u3")

        rows = await audit.tail(2)
        assert len(rows) == 2
        # Mock returns reversed order (most recent first)
        assert rows[0]["target"] == "A3"
        assert rows[1]["target"] == "A2"
    finally:
        await audit.close()


@pytest.mark.asyncio
async def test_status_api_audit_tail_endpoint():
    """GET /audit/tail?n=20 returns 200 OK with list of recent records."""
    from httpx import ASGITransport, AsyncClient
    from nexus.observability.status_api import app, context

    mock_client = MockPostgresClient()
    audit = AuditTrail(db_client=mock_client)
    await audit.initialize()
    await audit.record_approval("A1", "u1")

    prev_audit = context.audit_trail
    context.audit_trail = audit
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/audit/tail?n=10")
            assert resp.status_code == 200
            data = resp.json()
            assert isinstance(data, list)
            assert len(data) == 1
            assert data[0]["target"] == "A1"
    finally:
        context.audit_trail = prev_audit
        await audit.close()

