"""
NEXUS Root Cause Analysis Data Structures
=========================================
Represents typed diagnosis results from LangGraph multi-agent RCA.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

VALID_FAILURE_CLASSES = frozenset(
    {
        "bad_deploy",
        "resource_exhaustion",
        "dependency_failure",
        "config_error",
        "cascading_failure",
        "unknown",
    }
)

VALID_HEALING_LEVELS = (0, 1, 2, 3)


@dataclass
class RCAResult:
    """Output of the Multi-Agent RCA investigation."""

    root_cause: str
    failure_class: str
    healing_level: int = 1
    runbook_id: str | None = None  # Deprecated: kept for backwards compatibility
    confidence: float = 0.8
    reasoning: str = ""
    source: str = "langgraph_agent"
    actions_to_avoid: list[str] = field(default_factory=list)
    domain: str = "kubernetes"  # "kubernetes" | "aws" | "hybrid"
    suggested_action: str | None = None
    suggested_fix: str | None = None
    action_params: dict[str, Any] = field(default_factory=dict)
    rollback_plan: dict[str, Any] | None = None
    evidence_citations: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        self.healing_level = max(0, min(3, int(self.healing_level)))
        if self.failure_class not in VALID_FAILURE_CLASSES:
            self.failure_class = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_cause": self.root_cause,
            "failure_class": self.failure_class,
            "healing_level": self.healing_level,
            "runbook_id": self.runbook_id,
            "confidence": round(self.confidence, 3),
            "reasoning": self.reasoning,
            "source": self.source,
            "actions_to_avoid": self.actions_to_avoid,
            "domain": self.domain,
            "suggested_action": self.suggested_action,
            "suggested_fix": self.suggested_fix,
            "action_params": self.action_params,
            "rollback_plan": self.rollback_plan,
            "evidence_citations": self.evidence_citations,
        }

    def __str__(self) -> str:
        return (
            f"RCAResult(class={self.failure_class}, L{self.healing_level}, "
            f"confidence={self.confidence:.2f}, action={self.suggested_action}, "
            f"src={self.source})"
        )
