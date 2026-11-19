"""
NEXUS Outcome Store
====================
Read-only analytics layer over the AuditTrail PostgreSQL database.

Provides per-runbook aggregated statistics (success rate, false-heal rate,
mean time to heal) and system-level KPIs that feed the Knowledge Base
and the Runbook Advisor.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from nexus.core.database.connection.client import PostgresClient, get_database_client
from nexus.intelligence.knowledge.outcomes.outcome_models import (
    _COMPLETED,
    OutcomeRecord,
    RunbookStats,
    SystemKPIs,
)
from nexus.intelligence.knowledge.outcomes.outcome_queries import (
    compute_all_runbook_stats,
    compute_runbook_stats,
    compute_system_kpis,
)

logger = logging.getLogger(__name__)

__all__ = [
    "OutcomeRecord",
    "RunbookStats",
    "SystemKPIs",
    "OutcomeStore",
    "_COMPLETED",
]


class OutcomeStore:
    """
    Read-only analytics queries over the AuditTrail PostgreSQL database.

    Args:
        db_path: Deprecated argument kept for backwards compatibility.
        db_client: Optional PostgresClient instance.
    """

    def __init__(
        self,
        db_path: str | None = None,
        db_client: PostgresClient | None = None,
    ):
        self._db_path = db_path
        self._db_client = db_client

    async def connect(self) -> None:
        """Connect to PostgreSQL."""
        try:
            if self._db_client is None:
                self._db_client = await get_database_client()
            logger.info("[OutcomeStore] Connected to PostgreSQL")
        except Exception as exc:
            logger.warning(f"[OutcomeStore] Connection failed: {exc}")

    async def close(self) -> None:
        self._db_client = None

    async def __aenter__(self) -> OutcomeStore:
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _since_dt(self, days: int) -> datetime:
        """Datetime for N days ago (UTC)."""
        return datetime.now(timezone.utc) - timedelta(days=days)

    async def _execute(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        """Execute a SELECT and return results as list of dicts."""
        if not self._db_client:
            return []
        try:
            async with self._db_client.acquire() as conn:
                rows = await conn.fetch(sql, *params)
                return [dict(row) for row in rows]
        except Exception as exc:
            logger.warning(f"[OutcomeStore] Query error: {exc}")
            return []

    # ── Queries ───────────────────────────────────────────────────────────────

    async def get_recent_outcomes(self, limit: int = 200) -> list[OutcomeRecord]:
        """Return the N most recent non-pending audit records."""
        rows = await self._execute(
            """
            SELECT * FROM audit_trail
            WHERE execution_outcome != 'pending'
            ORDER BY timestamp DESC
            LIMIT $1
            """,
            limit,
        )
        return [OutcomeRecord.from_row(r) for r in rows]

    async def get_runbook_stats(self, runbook_id: str, days: int = 30) -> RunbookStats:
        """Compute aggregated statistics for one runbook over the last N days."""
        since = self._since_dt(days)
        rows = await self._execute(
            """
            SELECT execution_outcome, rollback_triggered
            FROM audit_trail
            WHERE runbook_id = $1 AND timestamp >= $2
            """,
            runbook_id,
            since,
        )
        return compute_runbook_stats(rows, runbook_id, days)

    async def get_all_runbook_stats(self, days: int = 30) -> dict[str, RunbookStats]:
        """Compute statistics for every runbook that has any record in the window."""
        since = self._since_dt(days)
        rows = await self._execute(
            """
            SELECT runbook_id, execution_outcome, rollback_triggered
            FROM audit_trail
            WHERE timestamp >= $1
            """,
            since,
        )
        return compute_all_runbook_stats(rows, days)

    async def get_system_kpis(self, days: int = 30) -> SystemKPIs:
        """Compute system-level KPIs across all runbooks."""
        since = self._since_dt(days)
        rows = await self._execute(
            """
            SELECT runbook_id, healing_level, execution_outcome
            FROM audit_trail
            WHERE execution_outcome != 'pending'
            AND timestamp >= $1
            """,
            since,
        )
        return compute_system_kpis(rows, days)

    async def get_outcome_timeseries(
        self, runbook_id: str, days: int = 7
    ) -> list[dict[str, Any]]:
        """Return a time-series of outcomes for a runbook."""
        since = self._since_dt(days)
        return await self._execute(
            """
            SELECT (timestamp::date)::text as date, execution_outcome, COUNT(*) as count
            FROM audit_trail
            WHERE runbook_id = $1 AND timestamp >= $2
            GROUP BY (timestamp::date)::text, execution_outcome
            ORDER BY date
            """,
            runbook_id,
            since,
        )

    async def get_targets_with_most_heals(
        self, days: int = 7, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Return the targets (namespace/resource) with most healing actions."""
        since = self._since_dt(days)
        return await self._execute(
            """
            SELECT target, runbook_id, COUNT(*) as heal_count,
                   SUM(CASE WHEN execution_outcome = 'success' THEN 1 ELSE 0 END) as successes
            FROM audit_trail
            WHERE timestamp >= $1 AND target IS NOT NULL
            GROUP BY target, runbook_id
            ORDER BY heal_count DESC
            LIMIT $2
            """,
            since,
            limit,
        )

    async def write_ppa_outcome(self, outcome: dict) -> None:
        """Record a PPA prediction outcome verdict (passthrough / no-op stub)."""
        logger.debug(
            f"[OutcomeStore] PPA outcome: verdict={outcome.get('verdict')} "
            f"for {outcome.get('deployment')}"
        )
