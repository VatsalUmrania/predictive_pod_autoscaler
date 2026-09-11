"""
NEXUS Outcome Store
====================
Read-only analytics layer over the AuditTrail PostgreSQL database.

Provides per-runbook aggregated statistics (success rate, false-heal rate,
mean time to heal) and system-level KPIs that feed the Knowledge Base
and the Runbook Advisor.

All queries are:
    • Read-only (never INSERT / UPDATE / DELETE)
    • Time-windowed (default last 30 days)
    • Graceful — return zero/empty on connection errors
    • Filtered: exclude 'pending' and 'governance_blocked' outcomes from rates
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from nexus.db.postgres import PostgresClient, get_database_client

logger = logging.getLogger(__name__)

# Outcomes that count as "completed" for success-rate calculation
_COMPLETED = ("success", "failed", "rolled_back", "skipped")


# Data structures
@dataclass
class OutcomeRecord:
    """Normalized view of one AuditTrail row."""

    action_id: str
    timestamp: str
    triggered_by: str
    runbook_id: str
    healing_level: int
    target: str
    execution_outcome: str
    rollback_triggered: bool
    incident_id: str | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> OutcomeRecord:
        ts = row.get("timestamp", "")
        if isinstance(ts, datetime):
            ts = ts.isoformat()
        return cls(
            action_id=str(row["action_id"]),
            timestamp=str(ts),
            triggered_by=str(row.get("triggered_by", "")),
            runbook_id=str(row.get("runbook_id", "")),
            healing_level=int(row.get("healing_level", 0)),
            target=str(row.get("target") or ""),
            execution_outcome=str(row.get("execution_outcome", "unknown")),
            rollback_triggered=bool(row.get("rollback_triggered", False)),
            incident_id=str(row.get("incident_id")) if row.get("incident_id") else None,
        )

    @property
    def is_success(self) -> bool:
        return self.execution_outcome == "success"

    @property
    def is_false_heal(self) -> bool:
        return self.execution_outcome in ("rolled_back", "failed")

    @property
    def is_completed(self) -> bool:
        return self.execution_outcome in _COMPLETED


@dataclass
class RunbookStats:
    """Aggregated outcome metrics for a single runbook over a time window."""

    runbook_id: str
    window_days: int = 30
    total: int = 0
    successes: int = 0
    failures: int = 0
    rolled_back: int = 0
    skipped: int = 0
    pending: int = 0

    @property
    def completed(self) -> int:
        return self.successes + self.failures + self.rolled_back + self.skipped

    @property
    def success_rate(self) -> float:
        return self.successes / self.completed if self.completed > 0 else 0.0

    @property
    def false_heal_rate(self) -> float:
        bad = self.failures + self.rolled_back
        return bad / self.completed if self.completed > 0 else 0.0

    @property
    def rollback_rate(self) -> float:
        return self.rolled_back / self.completed if self.completed > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "runbook_id": self.runbook_id,
            "window_days": self.window_days,
            "total_actions": self.total,
            "completed_actions": self.completed,
            "successes": self.successes,
            "failures": self.failures,
            "rolled_back": self.rolled_back,
            "skipped": self.skipped,
            "pending": self.pending,
            "success_rate": round(self.success_rate, 4),
            "false_heal_rate": round(self.false_heal_rate, 4),
            "rollback_rate": round(self.rollback_rate, 4),
        }


@dataclass
class SystemKPIs:
    """NEXUS system-level healing KPIs across all runbooks."""

    window_days: int = 30
    total_actions: int = 0
    total_successes: int = 0
    total_false_heals: int = 0
    total_rollbacks: int = 0
    autonomous_success_rate: float = 0.0
    false_heal_rate: float = 0.0
    mttr_seconds_avg: float = 0.0
    actions_by_level: dict[str, int] = field(default_factory=dict)
    actions_by_runbook: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_days": self.window_days,
            "total_actions": self.total_actions,
            "total_successes": self.total_successes,
            "total_false_heals": self.total_false_heals,
            "total_rollbacks": self.total_rollbacks,
            "autonomous_success_rate": round(self.autonomous_success_rate, 4),
            "false_heal_rate": round(self.false_heal_rate, 4),
            "mttr_seconds_avg": round(self.mttr_seconds_avg, 1),
            "actions_by_level": self.actions_by_level,
            "actions_by_runbook": self.actions_by_runbook,
        }


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

        stats = RunbookStats(runbook_id=runbook_id, window_days=days)
        for row in rows:
            stats.total += 1
            outcome = row.get("execution_outcome", "unknown")
            if outcome == "success":
                stats.successes += 1
            elif outcome == "failed":
                stats.failures += 1
            elif outcome == "rolled_back":
                stats.rolled_back += 1
            elif outcome == "skipped":
                stats.skipped += 1
            elif outcome == "pending":
                stats.pending += 1

        return stats

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

        agg: dict[str, RunbookStats] = {}
        for row in rows:
            rb_id = str(row["runbook_id"])
            if rb_id not in agg:
                agg[rb_id] = RunbookStats(runbook_id=rb_id, window_days=days)
            stats = agg[rb_id]
            outcome = row.get("execution_outcome", "unknown")
            stats.total += 1
            if outcome == "success":
                stats.successes += 1
            elif outcome == "failed":
                stats.failures += 1
            elif outcome == "rolled_back":
                stats.rolled_back += 1
            elif outcome == "skipped":
                stats.skipped += 1
            elif outcome == "pending":
                stats.pending += 1

        return agg

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

        kpis = SystemKPIs(window_days=days)
        for row in rows:
            kpis.total_actions += 1
            outcome = row.get("execution_outcome", "unknown")
            level = str(row.get("healing_level", "?"))
            rb_id = str(row.get("runbook_id", "unknown"))

            if outcome == "success":
                kpis.total_successes += 1
            elif outcome in ("failed", "rolled_back"):
                kpis.total_false_heals += 1
            if outcome == "rolled_back":
                kpis.total_rollbacks += 1

            kpis.actions_by_level[f"L{level}"] = (
                kpis.actions_by_level.get(f"L{level}", 0) + 1
            )
            kpis.actions_by_runbook[rb_id] = kpis.actions_by_runbook.get(rb_id, 0) + 1

        completed = kpis.total_successes + kpis.total_false_heals
        if completed > 0:
            kpis.autonomous_success_rate = kpis.total_successes / completed
            kpis.false_heal_rate = kpis.total_false_heals / completed

        return kpis

    async def get_outcome_timeseries(
        self, runbook_id: str, days: int = 7
    ) -> list[dict[str, Any]]:
        """
        Return a time-series of outcomes for a runbook.
        """
        since = self._since_dt(days)
        rows = await self._execute(
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
        return rows

    async def get_targets_with_most_heals(
        self, days: int = 7, limit: int = 10
    ) -> list[dict[str, Any]]:
        """
        Return the targets (namespace/resource) that received the most healing actions.
        """
        since = self._since_dt(days)
        rows = await self._execute(
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
        return rows

    async def write_ppa_outcome(self, outcome: dict) -> None:
        """Record a PPA prediction outcome verdict (passthrough / no-op stub)."""
        logger.debug(
            f"[OutcomeStore] PPA outcome: verdict={outcome.get('verdict')} "
            f"for {outcome.get('deployment')}"
        )
