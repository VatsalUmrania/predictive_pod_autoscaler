"""
Aggregators and analytics calculators for OutcomeStore.
"""

from __future__ import annotations

from typing import Any

from nexus.intelligence.knowledge.outcomes.outcome_models import (
    RunbookStats,
    SystemKPIs,
)


def compute_runbook_stats(
    rows: list[dict[str, Any]], runbook_id: str, days: int
) -> RunbookStats:
    """Compute aggregated statistics for one runbook from rows."""
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


def compute_all_runbook_stats(
    rows: list[dict[str, Any]], days: int
) -> dict[str, RunbookStats]:
    """Compute statistics for every runbook from audit rows."""
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


def compute_system_kpis(rows: list[dict[str, Any]], days: int) -> SystemKPIs:
    """Compute system-level KPIs across all runbook rows."""
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
