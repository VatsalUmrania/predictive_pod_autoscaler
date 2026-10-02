"""Data models and enums for Predictive Pod Autoscaling."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class PrescaleMode(str, Enum):
    SHADOW = "shadow"
    ADVISORY = "advisory"
    AUTONOMOUS = "autonomous"


@dataclass
class PrescaleDecision:
    """A single pre-scale recommendation."""

    decision_id: str
    deployment_name: str
    namespace: str
    endpoint: str
    current_rps: float
    predicted_rps: float
    horizon_minutes: int
    current_replicas: int
    recommended_replicas: int
    confidence: float
    mode: PrescaleMode
    db_table_trigger: str | None

    decided_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    verified_at: str | None = None
    actual_rps: float | None = None
    outcome: str | None = None  # "correct" | "false_positive" | "pending"
    executed: bool = False

    def mark_outcome(self, actual_rps: float, spike_threshold: float = 1.3) -> str:
        self.actual_rps = actual_rps
        self.verified_at = datetime.now(timezone.utc).isoformat()
        expected_spike = self.current_rps * spike_threshold
        self.outcome = "correct" if actual_rps >= expected_spike else "false_positive"
        return self.outcome


@dataclass
class PrecisionStats:
    """Rolling precision metrics over the last N decisions."""

    true_positives: int = 0
    false_positives: int = 0
    pending: int = 0

    @property
    def precision(self) -> float:
        total = self.true_positives + self.false_positives
        if total == 0:
            return 0.0
        return self.true_positives / total

    @property
    def sample_count(self) -> int:
        return self.true_positives + self.false_positives

    def __str__(self) -> str:
        return (
            f"precision={self.precision:.2f} "
            f"TP={self.true_positives} FP={self.false_positives} "
            f"pending={self.pending}"
        )
