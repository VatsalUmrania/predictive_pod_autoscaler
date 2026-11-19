"""
Data structures, thresholds, and delta calculation for KnowledgeBase.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.intelligence.knowledge.outcome_store import RunbookStats

# Thresholds for adjustment computation
_HIGH_PERFORMER_RATE = 0.85
_LOW_PERFORMER_RATE = 0.50
_MAX_POSITIVE_DELTA = +0.05
_MAX_NEGATIVE_DELTA = -0.10
_POSITIVE_TARGET_N = 10  # Evidence required for full positive boost
_NEGATIVE_TARGET_N = 5  # Evidence required for full negative penalty


def compute_delta(stats: RunbookStats) -> float:
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
