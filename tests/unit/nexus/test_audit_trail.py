"""Unit tests for nexus.governance.audit_trail.AuditTrail.

Covers the three changed write paths:
  - update_outcome()  (the SQL that was left broken by the recent edit —
    restore closes the ``await self._db.execute(...)`` call)
  - record_approval()  (new: human approve endpoint writes here)
  - record_rejection()  (new: human reject endpoint writes here)

Uses a real aiosqlite DB under tmp_path (no Redis / no K8s / no NATS).
"""

import json

import pytest

from nexus.governance.audit_trail import AuditTrail


@pytest.mark.asyncio
async def test_update_outcome_updates_pending_record(tmp_path):
    """update_outcome() must mutate an existing 'pending' row in place."""
    audit = AuditTrail(db_path=str(tmp_path / "audit.db"))
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
        # The restored bind — action_results JSON must round-trip (this is
        # exactly the line the broken edit deleted).
        assert json.loads(row["action_results"]) == [
            {"pod": "payments-api-xyz", "action": "restart"}
        ]
        assert json.loads(row["post_check_results"]) == {"healthy": True}
        assert row["rollback_triggered"] == 0
    finally:
        await audit.close()


@pytest.mark.asyncio
async def test_update_outcome_preserves_other_columns(tmp_path):
    """update_outcome() touches only the four outcome columns — runbook_id etc stay."""
    audit = AuditTrail(db_path=str(tmp_path / "audit.db"))
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
        assert rows[0]["rollback_triggered"] == 1
    finally:
        await audit.close()


@pytest.mark.asyncio
async def test_record_approval_persists_approved_row(tmp_path):
    """record_approval() writes an 'approved' audit record attributed to the user."""
    audit = AuditTrail(db_path=str(tmp_path / "audit.db"))
    await audit.initialize()
    try:
        returned = await audit.record_approval("APPROVAL-9F", "api_user")
        assert returned == "approve_APPROVAL-9F"

        rows = await audit.query_recent(limit=10)
        assert len(rows) == 1
        row = rows[0]
        assert row["action_id"] == "approve_APPROVAL-9F"
        assert row["execution_outcome"] == "approved"
        assert row["runbook_id"] == "system_approval"
        assert row["triggered_by"] == "human:api_user"
        assert row["target"] == "APPROVAL-9F"
    finally:
        await audit.close()


@pytest.mark.asyncio
async def test_record_rejection_persists_rejected_row(tmp_path):
    """record_rejection() writes a 'rejected' audit record."""
    audit = AuditTrail(db_path=str(tmp_path / "audit.db"))
    await audit.initialize()
    try:
        returned = await audit.record_rejection("APPROVAL-1A", "sre-oncall")
        assert returned == "reject_APPROVAL-1A"

        rows = await audit.query_recent(limit=10)
        assert len(rows) == 1
        row = rows[0]
        assert row["action_id"] == "reject_APPROVAL-1A"
        assert row["execution_outcome"] == "rejected"
        assert row["runbook_id"] == "system_rejection"
        assert row["triggered_by"] == "human:sre-oncall"
        assert row["target"] == "APPROVAL-1A"
    finally:
        await audit.close()


@pytest.mark.asyncio
async def test_record_rules_separately_queryable_by_runbook(tmp_path):
    """Both record_* rows land in the audit table and are queryable by runbook_id."""
    audit = AuditTrail(db_path=str(tmp_path / "audit.db"))
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
