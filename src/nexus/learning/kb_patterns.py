"""
Signal pattern and per-incident outcome persistence for KnowledgeBase.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from nexus.db.postgres import PostgresClient

logger = logging.getLogger(__name__)


async def record_signal_pattern(
    client: PostgresClient | None,
    signal_types: set[str],
    runbook_id: str,
    success: bool,
) -> None:
    """Record a signal_type combination and whether the runbook succeeded."""
    if not client:
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
    async with client.acquire() as conn:
        await conn.execute(sql, key, json.dumps(sorted(signal_types)), runbook_id, succ, now)


async def query_best_runbook_for_pattern(
    client: PostgresClient | None,
    signal_types: set[str],
) -> str | None:
    """Look up which runbook historically worked best for a given signal-type set."""
    if not client:
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
    async with client.acquire() as conn:
        row = await conn.fetchrow(sql, key)
        return str(row["runbook_id"]) if row else None


async def query_working_patterns(
    client: PostgresClient | None,
    min_success_rate: float = 0.80,
) -> list[dict[str, Any]]:
    """Return signal patterns that reliably led to successful healing."""
    if not client:
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
    async with client.acquire() as conn:
        rows = await conn.fetch(sql, min_success_rate)
        return [dict(r) for r in rows]


async def record_db_incident_outcome(
    client: PostgresClient | None,
    incident_id: str,
    runbook_id: str,
    action_type: str,
    resolved: bool,
    reason: str,
) -> None:
    """Record whether an incident was resolved by a specific runbook/action."""
    if not client:
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
        async with client.acquire() as conn:
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
