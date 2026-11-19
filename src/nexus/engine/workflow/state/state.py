"""
NEXUS LangGraph State Models
============================
Defines the central IncidentState TypedDict supporting dynamic topology discovery,
open failure classifications, schema-bound remediation planning, attribute-based
governance, and closed-loop verification.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, TypedDict

from pydantic import BaseModel, Field


class VerificationCondition(BaseModel):
    """Structured, safe verification contract (no Python eval)."""

    metric_name: str = Field(..., description="Target metric name to evaluate")
    operator: str = Field(..., description="Comparison operator (<, <=, >, >=, ==)")
    threshold: float = Field(..., description="Target numeric threshold")
    window_seconds: int = Field(
        default=60, description="Evaluation observation window in seconds"
    )


class PlanStep(BaseModel):
    """A single atomic action step within a remediation plan."""

    step_index: int = Field(default=0, description="Execution order sequence")
    tool_name: str = Field(
        ..., alias="action_tool", description="Registered NexusTool name to execute"
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        alias="action_args",
        description="Validated tool arguments",
    )
    description: str = Field(
        default="", description="Human-readable rationale for this action"
    )
    rollback_tool: str | None = Field(
        default=None, description="Tool name used to undo this action"
    )
    rollback_parameters: dict[str, Any] | None = Field(
        default=None, alias="rollback_args", description="Arguments to undo this action"
    )
    verification_tool: str | None = Field(
        default=None, description="Tool name used to verify remediation"
    )
    verification_parameters: dict[str, Any] = Field(
        default_factory=dict,
        alias="verification_args",
        description="Parameters for verification tool",
    )
    verification_condition: VerificationCondition | None = Field(
        default=None, description="Evaluation condition contract"
    )
    risk_level: str | None = Field(
        default=None, description="Deprecated; levels removed"
    )

    model_config = {"populate_by_name": True}


PlannedToolStep = PlanStep


class RemediationPlan(BaseModel):
    """Structured remediation plan model."""

    incident_id: str
    root_cause: str
    failure_mode: str
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    steps: list[PlanStep] = Field(
        default_factory=list, description="Ordered remediation steps"
    )
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
    rollback_tool: str | None = None
    rollback_parameters: dict[str, Any] | None = None


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
    risk_tier: str | None = None


class IncidentState(TypedDict, total=False):
    # Identity & Discovered Topology
    incident_id: str
    platform: str | None  # e.g., "k8s", "aws"
    namespace: str | None  # Discovered namespace or cloud region (None until verified)
    resource_kind: (
        str | None
    )  # Discovered controller kind (e.g., "Deployment", "StatefulSet", "Lambda")
    service_name: str | None  # Discovered owning controller/service name
    target_resource: (
        str | None
    )  # Canonical URI: "<namespace>/<resource_kind>/<service_name>"

    # Telemetry & Triage
    raw_events: list[dict[str, Any]]
    severity: str
    triage_status: str

    # ReAct Diagnosis (Plane 2)
    failure_class: str  # Open technical slug synthesized by diagnose_node
    suspected_cause: str
    confidence: float
    min_confidence_threshold: float
    rca_result: dict[str, Any]
    max_diagnose_steps: int

    # Schema-Bound Planning (Plane 2)
    remediation_plan: dict[str, Any]

    # Zero-Trust Governance & Human Approval (Plane 3)
    policy_decision: dict[str, Any]
    requires_approval: bool
    approved: bool | None
    approval_status: str  # "PENDING", "APPROVED", "REJECTED", "AUTO_APPROVED"
    approved_by: str | None
    approval_reason: str | None

    # Execution, Rollback & Closed-Loop Verification (Plane 4)
    execution_status: str  # "PENDING", "SUCCESS", "FAILED"
    execution_results: list[dict[str, Any]]
    remediation_attempts: int
    max_remediation_attempts: int
    verification_passed: bool | None
    verification_feedback: str | None
    rollback_available: bool
    rollback_results: list[dict[str, Any]]

    # Overall Lifecycle
    status: str
    error: str | None


IncidentGraphState = IncidentState
