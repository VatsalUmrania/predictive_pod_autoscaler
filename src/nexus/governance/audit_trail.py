"""
NEXUS Audit Trail
==================
Immutable, append-only log of every healing action taken by NEXUS.

Design:
    - Phase 1: SQLite (zero-ops, ships with Python, good for dev + single-node)
    - Phase 4+: PostgreSQL (multi-node, queryable by Grafana, supports concurrent writers)
    - Every record is written BEFORE an action executes (pre-write) and
      updated once the outcome is known (post-write)
    - Records are never deleted or mutated — only appended

Schema reflects the per-action contract defined in the Governance Plane:
    action_id            UUID, primary key
    timestamp            UTC ISO8601
    triggered_by         "seed_runbook_executor" | "orchestrator" | "human:<username>"
    runbook_id           e.g. "runbook_pod_crashloop_v1"
    healing_level        0 / 1 / 2 / 3
    target               "namespace/resource-name"
    pre_check_results    JSON
    execution_outcome    "success" | "failed" | "rolled_back" | "skipped"
    post_check_results   JSON
    rollback_triggered   bool
    incident_id          Correlation ID from the triggering IncidentEvent
    action_results       JSON list of per-action results
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

logger = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS audit_trail (
    action_id          TEXT PRIMARY KEY,
    timestamp          TEXT NOT NULL,
    triggered_by       TEXT NOT NULL,
    runbook_id         TEXT NOT NULL,
    healing_level      INTEGER NOT NULL DEFAULT 0,
    target             TEXT,
    pre_check_results  TEXT,
    execution_outcome  TEXT NOT NULL DEFAULT 'pending',
    post_check_results TEXT,
    rollback_triggered INTEGER NOT NULL DEFAULT 0,
    incident_id        TEXT,
    action_results     TEXT
);
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_audit_incident ON audit_trail(incident_id);",
    "CREATE INDEX IF NOT EXISTS idx_audit_runbook  ON audit_trail(runbook_id);",
    "CREATE INDEX IF NOT EXISTS idx_audit_ts       ON audit_trail(timestamp);",
]


class AuditTrail:
    """
    Async SQLite-backed audit trail for NEXUS healing actions.

    Usage:
        audit = AuditTrail("/var/nexus/audit.db")
        await audit.initialize()

        action_id = await audit.write_pending(runbook_id="...", ...)
        # ... execute action ...
        await audit.update_outcome(action_id, outcome="success", post_checks={...})
    """

    def __init__(self, db_path: str = "/tmp/nexus_audit.db"):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Create the database and table if they don't exist."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row

        async with self._lock:
            await self._db.execute(_CREATE_TABLE)
            for idx_sql in _CREATE_INDEXES:
                await self._db.execute(idx_sql)
            await self._db.commit()

        logger.info(f"[AuditTrail] Initialized at {self.db_path}")

    # ── Write ─────────────────────────────────────────────────────────────────

    async def write(
        self,
        *,
        triggered_by: str,
        runbook_id: str,
        healing_level: int = 0,
        target: Optional[str] = None,
        pre_check_results: Optional[Dict] = None,
        execution_outcome: str = "success",
        post_check_results: Optional[Dict] = None,
        rollback_triggered: bool = False,
        incident_id: Optional[str] = None,
        action_results: Optional[List[Dict]] = None,
        action_id: Optional[str] = None,
    ) -> str:
        """
        Write a complete audit record.

        Returns the action_id (UUID).
        """
        if not self._db:
            raise RuntimeError("AuditTrail not initialized. Call initialize() first.")

        record_id = action_id or str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()

        async with self._lock:
            await self._db.execute(
                """
                INSERT INTO audit_trail VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record_id,
                    timestamp,
                    triggered_by,
                    runbook_id,
                    healing_level,
                    target,
                    json.dumps(pre_check_results or {}),
                    execution_outcome,
                    json.dumps(post_check_results or {}),
                    1 if rollback_triggered else 0,
                    incident_id,
                    json.dumps(action_results or []),
                ),
            )
            await self._db.commit()

        logger.info(
            f"[AuditTrail] {record_id} | {runbook_id} | {execution_outcome} "
            f"| level={healing_level} | target={target}"
        )
        return record_id

    async def write_pending(self, *, triggered_by: str, runbook_id: str, **kwargs) -> str:
        """
        Write an audit record with outcome='pending'.
        Call update_outcome() once the action completes.

        Useful for pre-writing before execution so the record exists
        even if the executor crashes mid-action.
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
        post_check_results: Optional[Dict] = None,
        rollback_triggered: bool = False,
        action_results: Optional[List[Dict]] = None,
    ) -> None:
        """Update an existing pending record with the final outcome."""
        if not self._db:
            raise RuntimeError("AuditTrail not initialized.")

        async with self._lock:
            await self._db.execute(
                """
                UPDATE audit_trail
                SET execution_outcome  = ?,
                    post_check_results = ?,
                    rollback_triggered = ?,
                    action_results     = ?
                WHERE action_id = ?
                """,
                (
                    execution_outcome,
                    json.dumps(post_check_results or {}),
                    1 if rollback_triggered else 0,
                    json.dumps(action_results or []),
                    action_id,
                ),
            )
            await self._db.commit()

    # ── Query ─────────────────────────────────────────────────────────────────

    async def query_by_incident(self, incident_id: str) -> List[Dict[str, Any]]:
        """Return all audit records for a given correlation/incident ID."""
        if not self._db:
            raise RuntimeError("AuditTrail not initialized.")

        async with self._db.execute(
            "SELECT * FROM audit_trail WHERE incident_id = ? ORDER BY timestamp",
            (incident_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(row) for row in rows]

    async def query_recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return the N most recent audit records."""
        if not self._db:
            raise RuntimeError("AuditTrail not initialized.")

        async with self._db.execute(
            "SELECT * FROM audit_trail ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(row) for row in rows]

    async def query_by_runbook(self, runbook_id: str) -> List[Dict[str, Any]]:
        """Return all records for a given runbook (used by Learning Plane for scoring)."""
        if not self._db:
            raise RuntimeError("AuditTrail not initialized.")

        async with self._db.execute(
            "SELECT * FROM audit_trail WHERE runbook_id = ? ORDER BY timestamp",
            (runbook_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(row) for row in rows]

    async def runbook_success_rate(self, runbook_id: str) -> float:
        """
        Returns the success rate (0.0–1.0) for a given runbook.
        Used by the Learning Plane for runbook quality scoring.
        """
        records = await self.query_by_runbook(runbook_id)
        if not records:
            return 0.0
        successes = sum(1 for r in records if r["execution_outcome"] == "success")
        return successes / len(records)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            logger.info("[AuditTrail] Database connection closed")

    async def __aenter__(self) -> "AuditTrail":
        await self.initialize()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()
