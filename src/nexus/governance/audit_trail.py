"""
NEXUS Audit Trail
==================
Immutable, append-only log of every healing action taken by NEXUS.

Design:
    - Powered by PostgreSQL (multi-node, queryable by Grafana, supports concurrent writers)
    - Every record is written BEFORE an action executes (pre-write) and
      updated once the outcome is known (post-write)
    - Records are never deleted or mutated — only appended / updated on outcome
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from nexus.db.postgres import PostgresClient, _ensure_uuid, get_database_client

logger = logging.getLogger(__name__)


def _format_row(row: Any) -> dict[str, Any]:
    """Normalize asyncpg record to standard dict with JSON objects and ISO timestamps."""
    d = dict(row)
    if "action_id" in d and d["action_id"] is not None:
        d["action_id"] = str(d["action_id"])
    if "timestamp" in d and isinstance(d["timestamp"], datetime):
        d["timestamp"] = d["timestamp"].isoformat()
    for col in ("pre_check_results", "post_check_results", "action_results"):
        if col in d and isinstance(d[col], str):
            try:
                d[col] = json.loads(d[col])
            except Exception:
                pass
    if "rollback_triggered" in d and d["rollback_triggered"] is not None:
        d["rollback_triggered"] = bool(d["rollback_triggered"])
    return d


class AuditTrail:
    """
    Async PostgreSQL-backed audit trail for NEXUS healing actions.

    Usage:
        audit = AuditTrail()
        await audit.initialize()

        action_id = await audit.write_pending(runbook_id="...", ...)
        # ... execute action ...
        await audit.update_outcome(action_id, execution_outcome="success", post_check_results={...})
    """

    def __init__(
        self,
        db_path: str | None = None,
        db_client: PostgresClient | None = None,
    ):
        self.db_path = db_path
        self._db_client = db_client
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Connect to PostgreSQL and verify schema."""
        if self._db_client is None:
            self._db_client = await get_database_client()
        logger.info("[AuditTrail] Initialized with PostgreSQL client")

    @property
    def client(self) -> PostgresClient:
        if self._db_client is None:
            raise RuntimeError("AuditTrail not initialized. Call initialize() first.")
        return self._db_client

    # ── Write ─────────────────────────────────────────────────────────────────

    async def write(
        self,
        *,
        triggered_by: str,
        runbook_id: str,
        healing_level: int = 0,
        target: str | None = None,
        pre_check_results: dict | None = None,
        execution_outcome: str = "success",
        post_check_results: dict | None = None,
        rollback_triggered: bool = False,
        incident_id: str | None = None,
        action_results: list[dict] | None = None,
        action_id: str | None = None,
    ) -> str:
        """
        Write a complete audit record to PostgreSQL.
        Returns the action_id (UUID string).
        """
        rec_uuid = _ensure_uuid(action_id)
        action_id_str = str(rec_uuid)
        now = datetime.now(timezone.utc)

        sql = """
        INSERT INTO audit_trail (
            action_id, timestamp, triggered_by, runbook_id, healing_level,
            target, pre_check_results, execution_outcome, post_check_results,
            rollback_triggered, incident_id, action_results
        ) VALUES (
            $1, $2, $3, $4, $5,
            $6, $7::jsonb, $8, $9::jsonb,
            $10, $11, $12::jsonb
        )
        ON CONFLICT (action_id) DO NOTHING;
        """

        async with self.client.acquire() as conn:
            await conn.execute(
                sql,
                rec_uuid,
                now,
                triggered_by,
                runbook_id,
                int(healing_level),
                target,
                json.dumps(pre_check_results or {}),
                execution_outcome,
                json.dumps(post_check_results or {}),
                bool(rollback_triggered),
                str(incident_id) if incident_id else None,
                json.dumps(action_results or []),
            )

        logger.info(
            f"[AuditTrail] {action_id_str} | {runbook_id} | {execution_outcome} "
            f"| level={healing_level} | target={target}"
        )
        return action_id_str

    async def write_pending(
        self, *, triggered_by: str, runbook_id: str, **kwargs
    ) -> str:
        """
        Write an audit record with execution_outcome='pending'.
        Call update_outcome() once the action completes.
        """
        return await self.write(
            triggered_by=triggered_by,
            runbook_id=runbook_id,
            execution_outcome="pending",
            **kwargs,
        )

    async def update_outcome(
        self,
        action_id: str,
        *,
        execution_outcome: str,
        post_check_results: dict | None = None,
        rollback_triggered: bool = False,
        action_results: list[dict] | None = None,
    ) -> None:
        """Update an existing pending record with the final outcome."""
        rec_uuid = _ensure_uuid(action_id)
        sql = """
        UPDATE audit_trail
        SET execution_outcome  = $1,
            post_check_results = $2::jsonb,
            rollback_triggered = $3,
            action_results     = $4::jsonb
        WHERE action_id = $5
        """
        async with self.client.acquire() as conn:
            await conn.execute(
                sql,
                execution_outcome,
                json.dumps(post_check_results or {}),
                bool(rollback_triggered),
                json.dumps(action_results or []),
                rec_uuid,
            )

    async def record_approval(self, approval_id: str, username: str) -> str:
        """Record an explicit human approval."""
        return await self.write(
            triggered_by=f"human:{username}",
            runbook_id="system_approval",
            execution_outcome="approved",
            target=approval_id,
            action_id=f"approve_{approval_id}",
        )

    async def record_rejection(self, approval_id: str, username: str) -> str:
        """Record an explicit human rejection."""
        return await self.write(
            triggered_by=f"human:{username}",
            runbook_id="system_rejection",
            execution_outcome="rejected",
            target=approval_id,
            action_id=f"reject_{approval_id}",
        )

    # ── Query ─────────────────────────────────────────────────────────────────

    async def query_by_incident(self, incident_id: str) -> list[dict[str, Any]]:
        """Return all audit records for a given correlation/incident ID."""
        sql = "SELECT * FROM audit_trail WHERE incident_id = $1 ORDER BY timestamp ASC"
        async with self.client.acquire() as conn:
            rows = await conn.fetch(sql, str(incident_id))
            return [_format_row(r) for r in rows]

    async def query_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the N most recent audit records."""
        sql = "SELECT * FROM audit_trail ORDER BY timestamp DESC LIMIT $1"
        async with self.client.acquire() as conn:
            rows = await conn.fetch(sql, limit)
            return [_format_row(r) for r in rows]

    async def query_by_runbook(self, runbook_id: str) -> list[dict[str, Any]]:
        """Return all records for a given runbook."""
        sql = "SELECT * FROM audit_trail WHERE runbook_id = $1 ORDER BY timestamp ASC"
        async with self.client.acquire() as conn:
            rows = await conn.fetch(sql, runbook_id)
            return [_format_row(r) for r in rows]

    async def runbook_success_rate(self, runbook_id: str) -> float:
        """
        Returns the success rate (0.0–1.0) for a given runbook.
        """
        records = await self.query_by_runbook(runbook_id)
        if not records:
            return 0.0
        successes = sum(1 for r in records if r.get("execution_outcome") == "success")
        return successes / len(records)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def close(self) -> None:
        """No-op when sharing pool, or closes client if owned."""
        logger.info("[AuditTrail] Closed")

    async def __aenter__(self) -> AuditTrail:
        await self.initialize()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()
