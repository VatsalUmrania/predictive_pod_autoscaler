"""
NEXUS Outcome Store
====================
Read-only analytics layer over the AuditTrail SQLite database.

Provides per-runbook aggregated statistics (success rate, false-heal rate,
mean time to heal) and system-level KPIs that feed the Knowledge Base
and the Runbook Advisor.

Naming decisions:
    false_heal_rate   — fraction of executions where post-checks failed
                        (outcome = 'rolled_back' or 'failed')
    success_rate      — fraction where outcome = 'success'
    mttr_seconds      — avg seconds between write_pending and successful update_outcome
                        (proxied here by: not available from schema alone;
                         we measure cadence instead — see note)

Note on MTTR:
    The AuditTrail schema stores a single `timestamp` (write_pending time).
    True MTTR requires a completion timestamp (update_outcome time).
    In Phase 6 we approximate MTTR using the schema we have.
    Phase 8 will add a `completed_at` column to the schema.

All queries are:
    • Read-only (never INSERT / UPDATE / DELETE)
    • Time-windowed (default last 30 days)
    • Graceful — return zero/empty on connection errors
    • Filtered: exclude 'pending' and 'governance_blocked' outcomes from rates
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

logger = logging.getLogger(__name__)

# Outcomes that count as "completed" for success-rate calculation
_COMPLETED = ("success", "failed", "rolled_back", "skipped")


# ──────────────────────────────────────────────────────────────────────────────
# Data structures
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class OutcomeRecord:
    """Normalized view of one AuditTrail row."""
    action_id:          str
    timestamp:          str
    triggered_by:       str
    runbook_id:         str
    healing_level:      int
    target:             str
    execution_outcome:  str
    rollback_triggered: bool
    incident_id:        Optional[str]

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "OutcomeRecord":
        return cls(
            action_id          = row["action_id"],
            timestamp          = row["timestamp"],
            triggered_by       = row["triggered_by"],
            runbook_id         = row["runbook_id"],
            healing_level      = int(row.get("healing_level", 0)),
            target             = row.get("target") or "",
            execution_outcome  = row.get("execution_outcome", "unknown"),
            rollback_triggered = bool(row.get("rollback_triggered", 0)),
            incident_id        = row.get("incident_id"),
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
    """Aggregated healing statistics for one runbook over a time window."""
    runbook_id:      str
    window_days:     int
    total:           int = 0
    successes:       int = 0
    failures:        int = 0
    rolled_back:     int = 0
    skipped:         int = 0
    pending:         int = 0

    @property
    def completed(self) -> int:
        return self.successes + self.failures + self.rolled_back + self.skipped

    @property
    def success_rate(self) -> float:
        """Fraction of completed runs that succeeded."""
        if self.completed == 0:
            return 0.0
        return self.successes / self.completed

    @property
    def false_heal_rate(self) -> float:
        """Fraction of completed runs where healing failed (rolled back or errored)."""
        if self.completed == 0:
            return 0.0
        return (self.failures + self.rolled_back) / self.completed

    @property
    def rollback_rate(self) -> float:
        """Fraction of completed runs that triggered rollback."""
        if self.completed == 0:
            return 0.0
        return self.rolled_back / self.completed

    def to_dict(self) -> Dict[str, Any]:
        return {
            "runbook_id":      self.runbook_id,
            "window_days":     self.window_days,
            "total":           self.total,
            "completed":       self.completed,
            "successes":       self.successes,
            "failures":        self.failures,
            "rolled_back":     self.rolled_back,
            "pending":         self.pending,
            "success_rate":    round(self.success_rate, 3),
            "false_heal_rate": round(self.false_heal_rate, 3),
            "rollback_rate":   round(self.rollback_rate, 3),
        }

    def __str__(self) -> str:
        return (
            f"RunbookStats({self.runbook_id}: "
            f"rate={self.success_rate:.0%} "
            f"n={self.completed})"
        )


@dataclass
class SystemKPIs:
    """System-level healing performance KPIs."""
    total_actions:           int   = 0
    total_successes:         int   = 0
    total_false_heals:       int   = 0
    total_rollbacks:         int   = 0
    autonomous_success_rate: float = 0.0
    false_heal_rate:         float = 0.0
    actions_by_level:        Dict[str, int] = field(default_factory=dict)
    actions_by_runbook:      Dict[str, int] = field(default_factory=dict)
    window_days:             int  = 30

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_actions":           self.total_actions,
            "total_successes":         self.total_successes,
            "total_false_heals":       self.total_false_heals,
            "total_rollbacks":         self.total_rollbacks,
            "autonomous_success_rate": round(self.autonomous_success_rate, 3),
            "false_heal_rate":         round(self.false_heal_rate, 3),
            "actions_by_level":        self.actions_by_level,
            "actions_by_runbook":      self.actions_by_runbook,
            "window_days":             self.window_days,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Outcome Store
# ──────────────────────────────────────────────────────────────────────────────

class OutcomeStore:
    """
    Read-only analytics queries over the AuditTrail SQLite database.

    Args:
        db_path: Path to the AuditTrail database file. Reads NEXUS_AUDIT_DB_PATH
                 from environment if not provided (default: /tmp/nexus_audit.db).
    """

    def __init__(self, db_path: Optional[str] = None):
        import os
        self._db_path = db_path or os.getenv("NEXUS_AUDIT_DB_PATH", "/tmp/nexus_audit.db")
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        """Open a read-only connection to the AuditTrail database."""
        try:
            path = Path(self._db_path)
            if not path.exists():
                logger.warning(
                    f"[OutcomeStore] AuditTrail DB not found at {self._db_path} — "
                    f"learning queries will return empty results until healing actions occur"
                )
                return
            self._db = await aiosqlite.connect(self._db_path)
            self._db.row_factory = aiosqlite.Row
            logger.info(f"[OutcomeStore] Connected to {self._db_path}")
        except Exception as exc:
            logger.warning(f"[OutcomeStore] Connection failed: {exc}")

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> "OutcomeStore":
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _since_ts(self, days: int) -> str:
        """ISO timestamp for N days ago (UTC)."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        return cutoff.isoformat()

    async def _execute(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        """Execute a SELECT and return results as list of dicts."""
        if not self._db:
            return []
        try:
            async with self._db.execute(sql, params) as cur:
                rows = await cur.fetchall()
                return [dict(row) for row in rows]
        except Exception as exc:
            logger.warning(f"[OutcomeStore] Query error: {exc}")
            return []

    # ── Queries ───────────────────────────────────────────────────────────────

    async def get_recent_outcomes(self, limit: int = 200) -> List[OutcomeRecord]:
        """Return the N most recent non-pending audit records."""
        rows = await self._execute(
            """
            SELECT * FROM audit_trail
            WHERE execution_outcome != 'pending'
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [OutcomeRecord.from_row(r) for r in rows]

    async def get_runbook_stats(self, runbook_id: str, days: int = 30) -> RunbookStats:
        """Compute aggregated statistics for one runbook over the last N days."""
        since = self._since_ts(days)
        rows  = await self._execute(
            """
            SELECT execution_outcome, rollback_triggered
            FROM audit_trail
            WHERE runbook_id = ? AND timestamp >= ?
            """,
            (runbook_id, since),
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

    async def get_all_runbook_stats(self, days: int = 30) -> Dict[str, RunbookStats]:
        """Compute statistics for every runbook that has any record in the window."""
        since = self._since_ts(days)
        rows  = await self._execute(
            """
            SELECT runbook_id, execution_outcome, rollback_triggered
            FROM audit_trail
            WHERE timestamp >= ?
            """,
            (since,),
        )

        agg: Dict[str, RunbookStats] = {}
        for row in rows:
            rb_id = row["runbook_id"]
            if rb_id not in agg:
                agg[rb_id] = RunbookStats(runbook_id=rb_id, window_days=days)
            stats   = agg[rb_id]
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
        since = self._since_ts(days)
        rows  = await self._execute(
            """
            SELECT runbook_id, healing_level, execution_outcome
            FROM audit_trail
            WHERE execution_outcome != 'pending'
            AND timestamp >= ?
            """,
            (since,),
        )

        kpis = SystemKPIs(window_days=days)
        for row in rows:
            kpis.total_actions += 1
            outcome = row.get("execution_outcome", "unknown")
            level   = str(row.get("healing_level", "?"))
            rb_id   = row.get("runbook_id", "unknown")

            if outcome == "success":
                kpis.total_successes += 1
            elif outcome in ("failed", "rolled_back"):
                kpis.total_false_heals += 1
            if outcome == "rolled_back":
                kpis.total_rollbacks += 1

            kpis.actions_by_level[f"L{level}"]  = kpis.actions_by_level.get(f"L{level}", 0) + 1
            kpis.actions_by_runbook[rb_id]       = kpis.actions_by_runbook.get(rb_id, 0) + 1

        completed = kpis.total_successes + kpis.total_false_heals
        if completed > 0:
            kpis.autonomous_success_rate = kpis.total_successes / completed
            kpis.false_heal_rate         = kpis.total_false_heals / completed

        return kpis

    async def get_outcome_timeseries(
        self, runbook_id: str, days: int = 7
    ) -> List[Dict[str, Any]]:
        """
        Return a time-series of outcomes for a runbook — useful for detecting
        degradation trends (e.g., success rate falling over the last 7 days).
        """
        since = self._since_ts(days)
        return await self._execute(
            """
            SELECT date(timestamp) as date, execution_outcome, COUNT(*) as count
            FROM audit_trail
            WHERE runbook_id = ? AND timestamp >= ?
            GROUP BY date(timestamp), execution_outcome
            ORDER BY date
            """,
            (runbook_id, since),
        )

    async def get_targets_with_most_heals(self, days: int = 7, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Return the targets (namespace/resource) that received the most healing actions.
        High-count targets indicate chronic issues that runbooks alone cannot fix.
        """
        since = self._since_ts(days)
        return await self._execute(
            """
            SELECT target, runbook_id, COUNT(*) as heal_count,
                   SUM(CASE WHEN execution_outcome = 'success' THEN 1 ELSE 0 END) as successes
            FROM audit_trail
            WHERE timestamp >= ? AND target IS NOT NULL
            GROUP BY target, runbook_id
            ORDER BY heal_count DESC
            LIMIT ?
            """,
            (since, limit),
        )
