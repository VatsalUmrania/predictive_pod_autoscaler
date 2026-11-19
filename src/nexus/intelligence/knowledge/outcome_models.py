"""
Data structures and models for NEXUS Outcome Store.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Outcomes that count as "completed" for success-rate calculation
_COMPLETED = ("success", "failed", "rolled_back", "skipped")


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
