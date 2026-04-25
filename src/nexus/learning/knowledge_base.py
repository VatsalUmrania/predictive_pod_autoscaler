"""
NEXUS Knowledge Base
=====================
Stores learned confidence adjustments per runbook, derived from historical
healing outcomes supplied by the FeedbackLoop.

This is the memory of NEXUS — it closes the `act → learn` half of the
sense→reason→act→verify→learn loop.

Persistence:
    SQLite database (default: data/nexus_knowledge.db)
    Survives process restarts. Written by FeedbackLoop, read by ConfidenceScorer
    via the Orchestrator.

Confidence adjustment formula:
    high_performer  success_rate ≥ 0.85, n ≥ 10  → delta = +0.05
    mid_performer   success_rate 0.50–0.85         → delta ∝ (rate - 0.67)/0.18 × 0.05
    low_performer   success_rate < 0.50, n ≥ 5    → delta = −0.10

Evidence weighting (shrinkage):
    delta_applied = delta × min(n / target_n, 1.0)
    target_n = 10 for positive adjustments, 5 for negative
    Small sample sizes produce small adjustments — prevents over-fitting to noise.

Signal patterns:
    The knowledge base also records which signal-type combinations reliably
    led to successful heals. The RCAEngine can query this to supplement its
    prompt context (Phase 7 — LLM context enrichment).

Schema:
    confidence_adjustments: runbook_id → delta, evidence_count, success_rate
    signal_patterns:        signal_type_set → runbook_id, success_count, total_count
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Set

import aiosqlite

from nexus.learning.outcome_store import RunbookStats

logger = logging.getLogger(__name__)

_CREATE_ADJUSTMENTS = """
CREATE TABLE IF NOT EXISTS confidence_adjustments (
    runbook_id      TEXT PRIMARY KEY,
    delta           REAL    NOT NULL DEFAULT 0.0,
    evidence_count  INTEGER NOT NULL DEFAULT 0,
    success_rate    REAL    NOT NULL DEFAULT 0.0,
    false_heal_rate REAL    NOT NULL DEFAULT 0.0,
    last_updated    TEXT    NOT NULL
);
"""

_CREATE_PATTERNS = """
CREATE TABLE IF NOT EXISTS signal_patterns (
    pattern_key   TEXT    PRIMARY KEY,
    signal_types  TEXT    NOT NULL,
    runbook_id    TEXT    NOT NULL,
    success_count INTEGER NOT NULL DEFAULT 0,
    total_count   INTEGER NOT NULL DEFAULT 0,
    last_seen     TEXT    NOT NULL
);
"""

# Thresholds for adjustment computation
_HIGH_PERFORMER_RATE   = 0.85
_LOW_PERFORMER_RATE    = 0.50
_MAX_POSITIVE_DELTA    = +0.05
_MAX_NEGATIVE_DELTA    = -0.10
_POSITIVE_TARGET_N     = 10     # Evidence required for full positive boost
_NEGATIVE_TARGET_N     = 5      # Evidence required for full negative penalty


def _compute_delta(stats: RunbookStats) -> float:
    """
    Compute the confidence adjustment delta for a runbook.
    Returns a value in [_MAX_NEGATIVE_DELTA, _MAX_POSITIVE_DELTA].
    """
    n    = stats.completed
    rate = stats.success_rate

    if n == 0:
        return 0.0

    if rate >= _HIGH_PERFORMER_RATE:
        # Positive boost, scaled by evidence volume
        raw   = _MAX_POSITIVE_DELTA
        scale = min(n / _POSITIVE_TARGET_N, 1.0)
        return round(raw * scale, 4)

    if rate < _LOW_PERFORMER_RATE:
        # Negative penalty, scaled by evidence volume
        raw   = _MAX_NEGATIVE_DELTA
        scale = min(n / _NEGATIVE_TARGET_N, 1.0)
        return round(raw * scale, 4)

    # Linear interpolation through the middle band (0.50 → 0.85) → (0.0 → 0.0)
    # No adjustment in the middle — neither promote nor penalize
    return 0.0


# ──────────────────────────────────────────────────────────────────────────────
# Knowledge Base
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class AdjustmentRecord:
    """One row from the confidence_adjustments table."""
    runbook_id:      str
    delta:           float
    evidence_count:  int
    success_rate:    float
    false_heal_rate: float
    last_updated:    str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "runbook_id":      self.runbook_id,
            "delta":           self.delta,
            "evidence_count":  self.evidence_count,
            "success_rate":    round(self.success_rate, 3),
            "false_heal_rate": round(self.false_heal_rate, 3),
            "last_updated":    self.last_updated,
        }


class KnowledgeBase:
    """
    SQLite-backed store of learned confidence adjustments and signal patterns.

    Args:
        db_path: Path to the knowledge database file.
                 Reads NEXUS_KNOWLEDGE_DB_PATH from env (default: data/nexus_knowledge.db).
    """

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = db_path or os.getenv(
            "NEXUS_KNOWLEDGE_DB_PATH", "data/nexus_knowledge.db"
        )
        self._db: Optional[aiosqlite.Connection] = None

        # In-memory cache of adjustments (refreshed every update cycle)
        self._cache: Dict[str, float] = {}

    async def initialize(self) -> None:
        """Create tables if they don't exist."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row

        await self._db.execute(_CREATE_ADJUSTMENTS)
        await self._db.execute(_CREATE_PATTERNS)
        await self._db.commit()

        await self._refresh_cache()
        logger.info(
            f"[KnowledgeBase] Initialized at {self._db_path} — "
            f"{len(self._cache)} adjustment(s) loaded"
        )

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> "KnowledgeBase":
        await self.initialize()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    # ── Confidence adjustments ────────────────────────────────────────────────

    async def get_confidence_adjustment(self, runbook_id: str) -> float:
        """
        Return the learned confidence delta for a runbook.
        Served from in-memory cache — zero latency for the hot path.
        Returns 0.0 if no history is available.
        """
        return self._cache.get(runbook_id, 0.0)

    async def get_all_adjustments(self) -> Dict[str, float]:
        """Return the full cache of runbook_id → delta."""
        return dict(self._cache)

    async def update_from_stats(self, stats: RunbookStats) -> float:
        """
        Compute and persist the confidence adjustment for one runbook.
        Updates the in-memory cache immediately.
        Returns the computed delta.
        """
        if not self._db:
            return 0.0

        delta = _compute_delta(stats)
        now   = datetime.now(timezone.utc).isoformat()

        await self._db.execute(
            """
            INSERT INTO confidence_adjustments
                (runbook_id, delta, evidence_count, success_rate, false_heal_rate, last_updated)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(runbook_id) DO UPDATE SET
                delta           = excluded.delta,
                evidence_count  = excluded.evidence_count,
                success_rate    = excluded.success_rate,
                false_heal_rate = excluded.false_heal_rate,
                last_updated    = excluded.last_updated
            """,
            (
                stats.runbook_id,
                delta,
                stats.completed,
                stats.success_rate,
                stats.false_heal_rate,
                now,
            ),
        )
        await self._db.commit()

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

    async def bulk_update(self, all_stats: Dict[str, RunbookStats]) -> Dict[str, float]:
        """Update adjustments for all runbooks in one pass."""
        updates: Dict[str, float] = {}
        for rb_id, stats in all_stats.items():
            delta = await self.update_from_stats(stats)
            updates[rb_id] = delta
        return updates

    async def get_all_records(self) -> List[AdjustmentRecord]:
        """Return full adjustment table for dashboard queries."""
        if not self._db:
            return []
        async with self._db.execute(
            "SELECT * FROM confidence_adjustments ORDER BY delta DESC"
        ) as cur:
            rows = await cur.fetchall()
            return [
                AdjustmentRecord(
                    runbook_id      = r["runbook_id"],
                    delta           = r["delta"],
                    evidence_count  = r["evidence_count"],
                    success_rate    = r["success_rate"],
                    false_heal_rate = r["false_heal_rate"],
                    last_updated    = r["last_updated"],
                )
                for r in rows
            ]

    # ── Signal patterns ───────────────────────────────────────────────────────

    async def record_pattern(
        self,
        signal_types: Set[str],
        runbook_id:   str,
        success:      bool,
    ) -> None:
        """
        Record a signal_type combination and whether the associated runbook succeeded.
        Used for future LLM prompt enrichment (Phase 7).
        """
        if not self._db:
            return

        key  = "|".join(sorted(signal_types))
        now  = datetime.now(timezone.utc).isoformat()

        await self._db.execute(
            """
            INSERT INTO signal_patterns
                (pattern_key, signal_types, runbook_id, success_count, total_count, last_seen)
            VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(pattern_key) DO UPDATE SET
                success_count = success_count + ?,
                total_count   = total_count + 1,
                last_seen     = excluded.last_seen
            """,
            (
                key,
                json.dumps(sorted(signal_types)),
                runbook_id,
                1 if success else 0,
                now,
                1 if success else 0,
            ),
        )
        await self._db.commit()

    async def get_best_runbook_for_pattern(
        self, signal_types: Set[str]
    ) -> Optional[str]:
        """
        Look up which runbook historically worked best for a given signal-type set.
        Returns the runbook_id with highest success_count / total_count.
        """
        if not self._db:
            return None

        key = "|".join(sorted(signal_types))
        async with self._db.execute(
            """
            SELECT runbook_id,
                   CAST(success_count AS REAL) / MAX(total_count, 1) AS rate
            FROM signal_patterns
            WHERE pattern_key = ?
            ORDER BY rate DESC
            LIMIT 1
            """,
            (key,),
        ) as cur:
            row = await cur.fetchone()
            return row["runbook_id"] if row else None

    async def get_working_patterns(self, min_success_rate: float = 0.80) -> List[Dict[str, Any]]:
        """
        Return signal patterns that reliably led to successful healing.
        Used to enrich RCA prompts and advisor recommendations.
        """
        if not self._db:
            return []
        async with self._db.execute(
            """
            SELECT pattern_key, signal_types, runbook_id,
                   success_count, total_count,
                   CAST(success_count AS REAL) / MAX(total_count, 1) AS success_rate
            FROM signal_patterns
            WHERE total_count >= 3
            AND   CAST(success_count AS REAL) / MAX(total_count, 1) >= ?
            ORDER BY success_rate DESC
            """,
            (min_success_rate,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    # ── Cache refresh ─────────────────────────────────────────────────────────

    async def _refresh_cache(self) -> None:
        """Reload the in-memory confidence cache from the database."""
        if not self._db:
            return
        async with self._db.execute(
            "SELECT runbook_id, delta FROM confidence_adjustments"
        ) as cur:
            rows = await cur.fetchall()
            self._cache = {r["runbook_id"]: r["delta"] for r in rows}
