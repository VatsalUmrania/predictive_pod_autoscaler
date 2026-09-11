"""
NEXUS LangGraph State Models
============================
Defines typed schemas and the central IncidentGraphState for the unified
multi-platform LangGraph incident response pipeline.
"""

from __future__ import annotations

import operator
from datetime import datetime, timezone
from typing import Annotated, Any, TypedDict

from pydantic import BaseModel, Field


class PlanStep(BaseModel):
    """A single atomic action step within a remediation plan."""

    step_index: int = Field(default=0, description="Execution order sequence")
    tool_name: str = Field(..., description="Registered NexusTool name to execute")
    parameters: dict[str, Any] = Field(default_factory=dict, description="Validated tool arguments")
    description: str = Field(default="", description="Human-readable rationale for this action")
    rollback_tool: str | None = Field(default=None, description="Tool name used to undo this action")
    rollback_parameters: dict[str, Any] | None = Field(default=None, description="Arguments to undo this action")
    risk_level: str | None = Field(default=None, description="Deprecated; levels removed")


class RemediationPlan(BaseModel):
    """Structured remediation plan produced by the Planner Node."""

    incident_id: str
    root_cause: str
    failure_mode: str
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    steps: list[PlanStep] = Field(default_factory=list, description="Ordered remediation steps (max 3)")
    requires_approval: bool = True
    approval_reason: str | None = None
    pre_flight_conditions: dict[str, Any] = Field(default_factory=dict)
    expected_slo_target: str | None = None


class ExecutionRecord(BaseModel):
    """Audit record for an executed plan step."""

    step_index: int
    tool_name: str
    success: bool
    result_data: Any | None = None
    error: str | None = None
    snapshot_id: str | None = None
    executed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: float = 0.0


class GovernanceVerdict(BaseModel):
    """Consolidated safety and policy verdict evaluated before execution."""

    allowed: bool = True
    requires_human_approval: bool = True
    circuit_breaker_open: bool = False
    cooldown_active: bool = False
    live_state_valid: bool = True
    self_healed: bool = False
    reasons: list[str] = Field(default_factory=list)
    policy_checks: dict[str, Any] = Field(default_factory=dict)
    risk_tier: str | None = Field(default=None, description="Deprecated; levels removed")



class IncidentGraphState(TypedDict):
    """
    Central LangGraph state dictionary.
    Threaded through every node in the governed state machine.
    """

    incident_id: str
    fsm_state: str
    events: list[dict[str, Any]]
    platform: str
    target: dict[str, Any]
    severity: str

    # 1. Deterministic Telemetry (NO ReAct loop!)
    telemetry: dict[str, Any]

    # 2. Structured Diagnosis (Validated RCA & Reflexion)
    diagnosis: dict[str, Any]
    diagnostic_reflections: Annotated[list[dict[str, Any]], operator.add]

    # 3. Remediation Plan & Adaptive History
    plan: dict[str, Any] | None
    remediation_history: Annotated[list[dict[str, Any]], operator.add]

    # 4. Governance & Safety Verdict
    governance: dict[str, Any] | None

    # 5. Approval Info
    approval_id: str | None
    approval_decision: str | None  # "approved", "rejected", "timeout", None

    # 6. Execution & Rollback
    execution_records: Annotated[list[dict[str, Any]], operator.add]
    rollback_executed: bool
    rollback_records: Annotated[list[dict[str, Any]], operator.add]

    # 7. Verification & Post-check
    verification: dict[str, Any] | None
    retry_count: int
    max_retries: int

    # 8. Observability & Messages
    audit_log: Annotated[list[dict[str, Any]], operator.add]
    messages: Annotated[list[Any], operator.add]
    error_message: str | None
    resolved: bool
    escalated: bool

