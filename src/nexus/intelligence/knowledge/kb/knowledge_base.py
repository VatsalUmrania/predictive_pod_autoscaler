"""
NEXUS Knowledge Base
====================
Stores learned confidence adjustments per runbook, derived from historical
healing outcomes supplied by the FeedbackLoop.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from nexus.core.database.connection.client import PostgresClient, get_database_client
from nexus.intelligence.knowledge.kb.kb_models import (
    AdjustmentRecord,
    compute_delta,
)
from nexus.intelligence.knowledge.outcomes.outcome_store import RunbookStats

logger = logging.getLogger(__name__)

__all__ = ["AdjustmentRecord", "KnowledgeBase", "compute_delta"]


class KnowledgeBase:
    """PostgreSQL-backed store of learned confidence adjustments and signal patterns."""

    def __init__(
        self,
        db_path: str | None = None,
        db_client: PostgresClient | None = None,
    ):
        self._db_path = db_path
        self._db_client = db_client
        self._cache: dict[str, float] = {}

    async def initialize(self) -> None:
        """Connect to PostgreSQL and refresh cache."""
        if self._db_client is None:
            self._db_client = await get_database_client()
        await self._refresh_cache()
        logger.info(
            f"[KnowledgeBase] Initialized with PostgreSQL — {len(self._cache)} adjustment(s) loaded"
        )

    async def close(self) -> None:
        self._db_client = None

    async def __aenter__(self) -> KnowledgeBase:
        await self.initialize()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    async def get_confidence_adjustment(self, runbook_id: str) -> float:
        return self._cache.get(runbook_id, 0.0)

    async def get_all_adjustments(self) -> dict[str, float]:
        return dict(self._cache)

    async def update_from_stats(self, stats: RunbookStats) -> float:
        if not self._db_client:
            return 0.0

        delta = compute_delta(stats)
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
                f"[KnowledgeBase] {direction} {stats.runbook_id}: delta={delta:+.3f} rate={stats.success_rate:.0%} n={stats.completed}"
            )
        return delta

    async def bulk_update(self, all_stats: dict[str, RunbookStats]) -> dict[str, float]:
        updates: dict[str, float] = {}
        for rb_id, stats in all_stats.items():
            delta = await self.update_from_stats(stats)
            updates[rb_id] = delta
        return updates

    async def get_all_records(self) -> list[AdjustmentRecord]:
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

    async def record_pattern(
        self, signal_types: set[str], runbook_id: str, success: bool
    ) -> None:
        pass

    async def get_best_runbook_for_pattern(self, signal_types: set[str]) -> str | None:
        return None

    async def get_working_patterns(
        self, min_success_rate: float = 0.80
    ) -> list[dict[str, Any]]:
        return []

    async def _refresh_cache(self) -> None:
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

    async def record_incident_outcome(
        self,
        incident_id: str,
        runbook_id: str,
        action_type: str,
        resolved: bool,
        reason: str,
    ) -> None:
        pass
