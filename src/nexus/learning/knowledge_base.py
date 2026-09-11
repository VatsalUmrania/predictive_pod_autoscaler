"""
NEXUS Knowledge Base
=====================
Stores learned confidence adjustments per runbook, derived from historical
healing outcomes supplied by the FeedbackLoop.

Persistence:
    PostgreSQL database. Written by FeedbackLoop, read by ConfidenceScorer
    via the Orchestrator.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from nexus.db.postgres import PostgresClient, get_database_client
from nexus.learning.outcome_store import RunbookStats

logger = logging.getLogger(__name__)

# Thresholds for adjustment computation
_HIGH_PERFORMER_RATE = 0.85
_LOW_PERFORMER_RATE = 0.50
_MAX_POSITIVE_DELTA = +0.05
_MAX_NEGATIVE_DELTA = -0.10
_POSITIVE_TARGET_N = 10  # Evidence required for full positive boost
_NEGATIVE_TARGET_N = 5   # Evidence required for full negative penalty


def _compute_delta(stats: RunbookStats) -> float:
    """
    Compute the confidence adjustment delta for a runbook.
    Returns a value in [_MAX_NEGATIVE_DELTA, _MAX_POSITIVE_DELTA].
    """
    n = stats.completed
    rate = stats.success_rate

    if n == 0:
        return 0.0

    if rate >= _HIGH_PERFORMER_RATE:
        raw = _MAX_POSITIVE_DELTA
        scale = min(n / _POSITIVE_TARGET_N, 1.0)
        return round(raw * scale, 4)

    if rate < _LOW_PERFORMER_RATE:
        raw = _MAX_NEGATIVE_DELTA
        scale = min(n / _NEGATIVE_TARGET_N, 1.0)
        return round(raw * scale, 4)

    return 0.0


@dataclass
class AdjustmentRecord:
    """One row from the confidence_adjustments table."""

    runbook_id: str
    delta: float
    evidence_count: int
    success_rate: float
    false_heal_rate: float
    last_updated: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "runbook_id": self.runbook_id,
            "delta": self.delta,
            "evidence_count": self.evidence_count,
            "success_rate": round(self.success_rate, 3),
            "false_heal_rate": round(self.false_heal_rate, 3),
            "last_updated": self.last_updated,
        }


class KnowledgeBase:
    """
    PostgreSQL-backed store of learned confidence adjustments and signal patterns.

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
        # In-memory cache of adjustments (refreshed every update cycle)
        self._cache: dict[str, float] = {}

    async def initialize(self) -> None:
        """Connect to PostgreSQL and refresh cache."""
        if self._db_client is None:
            self._db_client = await get_database_client()

        await self._refresh_cache()
        logger.info(
            f"[KnowledgeBase] Initialized with PostgreSQL — "
            f"{len(self._cache)} adjustment(s) loaded"
        )

    async def close(self) -> None:
        self._db_client = None

    async def __aenter__(self) -> KnowledgeBase:
        await self.initialize()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    # ── Confidence adjustments ────────────────────────────────────────────────

    async def get_confidence_adjustment(self, runbook_id: str) -> float:
        """
        Return the learned confidence delta for a runbook.
        Served from in-memory cache — zero latency for the hot path.
        """
        return self._cache.get(runbook_id, 0.0)

    async def get_all_adjustments(self) -> dict[str, float]:
        """Return the full cache of runbook_id → delta."""
        return dict(self._cache)

    async def update_from_stats(self, stats: RunbookStats) -> float:
        """
        Compute and persist the confidence adjustment for one runbook.
        Updates the in-memory cache immediately.
        """
        if not self._db_client:
            return 0.0

        delta = _compute_delta(stats)
        now = datetime.now(timezone.utc)

        sql = """
        INSERT INTO confidence_adjustments (
            runbook_id, delta, evidence_count, success_rate, false_heal_rate, last_updated
        ) VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT(runbook_id) DO UPDATE SET
            delta           = EXCLUDED.delta,
            evidence_count  = EXCLUDED.evidence_count,
            success_rate    = EXCLUDED.success_rate,
            false_heal_rate = EXCLUDED.false_heal_rate,
            last_updated    = EXCLUDED.last_updated
        """
        async with self._db_client.acquire() as conn:
            await conn.execute(
                sql,
                stats.runbook_id,
                float(delta),
                int(stats.completed),
                float(stats.success_rate),
                float(stats.false_heal_rate),
                now,
            )

        self._cache[stats.runbook_id] = delta

        if delta != 0.0:
            direction = "↑" if delta > 0 else "↓"
            logger.info(
                f"[KnowledgeBase] {direction} {stats.runbook_id}: "
                f"delta={delta:+.3f} "
                f"rate={stats.success_rate:.0%} "
                f"n={stats.completed}"
            )

        return delta

    async def bulk_update(self, all_stats: dict[str, RunbookStats]) -> dict[str, float]:
        """Update adjustments for all runbooks in one pass."""
        updates: dict[str, float] = {}
        for rb_id, stats in all_stats.items():
            delta = await self.update_from_stats(stats)
            updates[rb_id] = delta
        return updates

    async def get_all_records(self) -> list[AdjustmentRecord]:
        """Return full adjustment table for dashboard queries."""
        if not self._db_client:
            return []
        async with self._db_client.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM confidence_adjustments ORDER BY delta DESC"
            )
            records = []
            for r in rows:
                lu = r["last_updated"]
                if isinstance(lu, datetime):
                    lu = lu.isoformat()
                records.append(
                    AdjustmentRecord(
                        runbook_id=str(r["runbook_id"]),
                        delta=float(r["delta"]),
                        evidence_count=int(r["evidence_count"]),
                        success_rate=float(r["success_rate"]),
                        false_heal_rate=float(r["false_heal_rate"]),
                        last_updated=str(lu),
                    )
                )
            return records

    # ── Signal patterns ───────────────────────────────────────────────────────

    async def record_pattern(
        self,
        signal_types: set[str],
        runbook_id: str,
        success: bool,
    ) -> None:
        """
        Record a signal_type combination and whether the associated runbook succeeded.
        """
        if not self._db_client:
            return

        key = "|".join(sorted(signal_types))
        now = datetime.now(timezone.utc)
        succ = 1 if success else 0

        sql = """
        INSERT INTO signal_patterns (
            pattern_key, signal_types, runbook_id, success_count, total_count, last_seen
        ) VALUES ($1, $2, $3, $4, 1, $5)
        ON CONFLICT(pattern_key) DO UPDATE SET
            success_count = signal_patterns.success_count + EXCLUDED.success_count,
            total_count   = signal_patterns.total_count + 1,
            last_seen     = EXCLUDED.last_seen
        """
        async with self._db_client.acquire() as conn:
            await conn.execute(
                sql,
                key,
                json.dumps(sorted(signal_types)),
                runbook_id,
                succ,
                now,
            )

    async def get_best_runbook_for_pattern(self, signal_types: set[str]) -> str | None:
        """
        Look up which runbook historically worked best for a given signal-type set.
        """
        if not self._db_client:
            return None

        key = "|".join(sorted(signal_types))
        sql = """
        SELECT runbook_id,
               CAST(success_count AS REAL) / GREATEST(total_count, 1) AS rate
        FROM signal_patterns
        WHERE pattern_key = $1
        ORDER BY rate DESC
        LIMIT 1
        """
        async with self._db_client.acquire() as conn:
            row = await conn.fetchrow(sql, key)
            return str(row["runbook_id"]) if row else None

    async def get_working_patterns(
        self, min_success_rate: float = 0.80
    ) -> list[dict[str, Any]]:
        """
        Return signal patterns that reliably led to successful healing.
        """
        if not self._db_client:
            return []

        sql = """
        SELECT pattern_key, signal_types, runbook_id,
               success_count, total_count,
               CAST(success_count AS REAL) / GREATEST(total_count, 1) AS success_rate
        FROM signal_patterns
        WHERE total_count >= 3
        AND   CAST(success_count AS REAL) / GREATEST(total_count, 1) >= $1
        ORDER BY success_rate DESC
        """
        async with self._db_client.acquire() as conn:
            rows = await conn.fetch(sql, min_success_rate)
            return [dict(r) for r in rows]

    # ── Cache refresh ─────────────────────────────────────────────────────────

    async def _refresh_cache(self) -> None:
        """Reload the in-memory confidence cache from the database."""
        if not self._db_client:
            return
        try:
            async with self._db_client.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT runbook_id, delta FROM confidence_adjustments"
                )
                self._cache = {str(r["runbook_id"]): float(r["delta"]) for r in rows}
        except Exception as exc:
            logger.warning(f"[KnowledgeBase] Cache refresh failed: {exc}")

    # ── Per-incident outcome recording (P3b) ──────────────────────────────────

    async def record_incident_outcome(
        self,
        incident_id: str,
        runbook_id: str,
        action_type: str,
        resolved: bool,
        reason: str,
    ) -> None:
        """
        Record whether an incident was resolved by a specific runbook/action.
        """
        if not self._db_client:
            return
        now = datetime.now(timezone.utc)
        try:
            sql = """
            INSERT INTO incident_outcomes (
                incident_id, runbook_id, action_type, resolved, reason, recorded_at
            ) VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT(incident_id) DO UPDATE SET
                runbook_id  = EXCLUDED.runbook_id,
                action_type = EXCLUDED.action_type,
                resolved    = EXCLUDED.resolved,
                reason      = EXCLUDED.reason,
                recorded_at = EXCLUDED.recorded_at
            """
            async with self._db_client.acquire() as conn:
                await conn.execute(
                    sql,
                    str(incident_id),
                    runbook_id,
                    action_type,
                    bool(resolved),
                    reason,
                    now,
                )
            logger.debug(
                f"[KnowledgeBase] Recorded incident outcome: "
                f"{incident_id} runbook={runbook_id} resolved={resolved} reason={reason}"
            )
        except Exception as exc:
            logger.warning(f"[KnowledgeBase] record_incident_outcome error: {exc}")
