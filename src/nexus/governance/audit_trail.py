"""NEXUS Audit Trail.

Immutable, append-only log of every healing action taken by NEXUS.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from nexus.db.client import PostgresClient, get_database_client
from nexus.db.utils import _ensure_uuid
from nexus.governance.audit_format import format_audit_row

logger = logging.getLogger(__name__)
_format_row = format_audit_row


class AuditTrail:
    """Async PostgreSQL-backed audit trail for NEXUS healing actions."""

    def __init__(
        self,
        db_path: str | None = None,
        db_client: PostgresClient | None = None,
    ):
        self.db_path = db_path
        self._db_client = db_client
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._db_client is None:
            self._db_client = await get_database_client()
        logger.info("[AuditTrail] Initialized with PostgreSQL client")

    @property
    def client(self) -> PostgresClient:
        if self._db_client is None:
            raise RuntimeError("AuditTrail not initialized. Call initialize() first.")
        return self._db_client

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
                json.dumps(pre_check_results or {}, default=str),
                execution_outcome,
                json.dumps(post_check_results or {}, default=str),
                bool(rollback_triggered),
                str(incident_id) if incident_id else None,
                json.dumps(action_results or [], default=str),
            )

        logger.info(
            f"[AuditTrail] {action_id_str} | {runbook_id} | {execution_outcome} | level={healing_level} | target={target}"
        )
        return action_id_str

    async def write_pending(
        self, *, triggered_by: str, runbook_id: str, **kwargs
    ) -> str:
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
                json.dumps(post_check_results or {}, default=str),
                bool(rollback_triggered),
                json.dumps(action_results or [], default=str),
                rec_uuid,
            )

    async def record_approval(self, approval_id: str, username: str) -> str:
        return await self.write(
            triggered_by=f"human:{username}",
            runbook_id="system_approval",
            execution_outcome="approved",
            target=approval_id,
            action_id=f"approve_{approval_id}",
        )

    async def record_rejection(self, approval_id: str, username: str) -> str:
        return await self.write(
            triggered_by=f"human:{username}",
            runbook_id="system_rejection",
            execution_outcome="rejected",
            target=approval_id,
            action_id=f"reject_{approval_id}",
        )

    async def query_by_incident(self, incident_id: str) -> list[dict[str, Any]]:
        sql = "SELECT * FROM audit_trail WHERE incident_id = $1 ORDER BY timestamp ASC"
        async with self.client.acquire() as conn:
            rows = await conn.fetch(sql, str(incident_id))
            return [format_audit_row(r) for r in rows]

    async def query_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM audit_trail ORDER BY timestamp DESC LIMIT $1"
        async with self.client.acquire() as conn:
            rows = await conn.fetch(sql, limit)
            return [format_audit_row(r) for r in rows]

    async def tail(self, n: int = 20) -> list[dict[str, Any]]:
        return await self.query_recent(limit=n)

    async def query_by_runbook(self, runbook_id: str) -> list[dict[str, Any]]:
        sql = "SELECT * FROM audit_trail WHERE runbook_id = $1 ORDER BY timestamp ASC"
        async with self.client.acquire() as conn:
            rows = await conn.fetch(sql, runbook_id)
            return [format_audit_row(r) for r in rows]

    async def runbook_success_rate(self, runbook_id: str) -> float:
        records = await self.query_by_runbook(runbook_id)
        if not records:
            return 0.0
        successes = sum(1 for r in records if r.get("execution_outcome") == "success")
        return successes / len(records)

    async def close(self) -> None:
        logger.info("[AuditTrail] Closed")

    async def __aenter__(self) -> AuditTrail:
        await self.initialize()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()
