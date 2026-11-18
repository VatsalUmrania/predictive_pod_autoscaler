"""
NEXUS Root Cause Analysis & Verification Data Structures
========================================================
Pydantic schemas for structured synthesis of Root Cause Analysis (RCAResult)
and Closed-Loop Verification (VerificationResult) with open failure slugs
and dynamically discovered target workload topology.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DiscoveredTarget(BaseModel):
    platform: str = Field(
        description="Target infrastructure platform discovered via tools (e.g., 'k8s' or 'aws')."
    )
    namespace_or_region: str = Field(
        description="Exact Kubernetes namespace or AWS region where the failing workload resides."
    )
    resource_kind: str = Field(
        description="Owning controller kind (e.g., 'Deployment', 'StatefulSet', 'DaemonSet', 'Lambda', 'ECSService')."
    )
    resource_name: str = Field(
        description="Exact name of the owning controller/service discovered by tracing ownerReferences or resource metadata."
    )


class RCAResult(BaseModel):
    target: DiscoveredTarget = Field(
        description="Authoritative target workload identity discovered via observation tools."
    )
    failure_type: str = Field(
        description="Concise, descriptive technical slug for the failure mode (e.g., 'readiness_probe_container_exit_zero', 'oom_killed_memory_limit')."
    )
    root_cause_summary: str = Field(
        description="Detailed technical explanation of why the resource is failing, citing concrete tool evidence."
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence score in the diagnosed root cause between 0.0 and 1.0.",
    )
    evidence: list[str] = Field(
        default_factory=list,
        description="Specific log lines, probe definitions, exit codes, or spec fields observed via tools.",
    )
    can_auto_remediate: bool = Field(
        description="True if available mutation tools can remediate or mitigate this root cause."
    )
    recommended_strategy: str = Field(
        description="Clear remediation instructions for the planner agent specifying what resource and spec fields to mutate."
    )


class VerificationResult(BaseModel):
    verification_passed: bool = Field(
        description="True only if live tool observations confirm the workload is healthy, ready, and stable."
    )
    observed_state_summary: str = Field(
        description="Summary of live post-remediation state (e.g., readyReplicas, pod statuses, probe health, error rates)."
    )
    failure_feedback: str | None = Field(
        default=None,
        description="If verification_passed is False, detailed explanation of what is still failing so diagnose_node can re-evaluate.",
    )
