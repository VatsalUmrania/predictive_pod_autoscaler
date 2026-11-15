"""
NEXUS Root Cause Analysis Data Structures
=========================================
Represents typed diagnosis results and evidence-backed RCAReport models
from the autonomous ReAct investigator loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

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


class EvidenceCitation(BaseModel):
    tool_name: str = Field(
        description="Exact read-only tool called during investigation"
    )
    tool_arguments: dict[str, Any] = Field(description="Arguments passed to the tool")
    key_finding: str = Field(
        description="Concrete metric value, log line, or config state returned by the tool"
    )


class RCAReport(BaseModel):
    root_cause_summary: str = Field(
        description="Precise technical explanation of the root cause"
    )
    affected_resource_urn: str = Field(
        description="Primary failing resource ARN or K8s URI"
    )
    provider: str = Field(description="'aws' or 'kubernetes'")
    confidence: float = Field(
        ge=0.0, le=1.0, description="Evidence-backed confidence score"
    )
    hypotheses_evaluated: list[str] = Field(
        description="Hypotheses tested and either confirmed or falsified"
    )
    evidence_chain: list[EvidenceCitation] = Field(
        min_length=1, description="Verified tool outputs proving the root cause"
    )
    pre_incident_resource_state: dict[str, Any] = Field(
        default_factory=dict,
        description="Current live configuration parameters of the failing resource needed to construct a safe rollback",
    )
    requires_remediation: bool = Field(default=True)


@dataclass
class RCAResult:
    """Legacy backward-compatible output of RCA investigation."""

    root_cause: str
    failure_class: str
    healing_level: int = 1
    runbook_id: str | None = None
    confidence: float = 0.8
    reasoning: str = ""
    source: str = "langgraph_agent"
    actions_to_avoid: list[str] = field(default_factory=list)
    domain: str = "kubernetes"
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
            "suggested_action": self.suggested_action,
            "suggested_fix": self.suggested_fix,
            "action_params": self.action_params,
            "rollback_plan": self.rollback_plan,
            "evidence_citations": self.evidence_citations,
        }
