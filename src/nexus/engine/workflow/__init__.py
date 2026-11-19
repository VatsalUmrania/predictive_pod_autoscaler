"""
NEXUS LangGraph Incident Automation
===================================
A governed, multi-platform, neuro-symbolic agent architecture for autonomous
infrastructure incident triage, diagnosis, remediation, verification, and rollback.
"""

from __future__ import annotations

from typing import Any

from nexus.engine.platforms import (
    AWSPlatformAdapter,
    BasePlatformAdapter,
    HealthCheckResult,
    K8sPlatformAdapter,
    LiveStateSnapshot,
    PlatformRegistry,
    PlatformTelemetry,
    RollbackSnapshot,
    TargetResource,
    get_platform_registry,
)
from nexus.engine.workflow.graph.checkpointer import GraphCheckpointStore
from nexus.engine.workflow.state.state import (
    ExecutionRecord,
    GovernanceVerdict,
    IncidentGraphState,
    PlanStep,
    RemediationPlan,
)

__all__ = [
    "IncidentWorkflow",
    "build_incident_graph",
    "IncidentGraphState",
    "PlanStep",
    "RemediationPlan",
    "ExecutionRecord",
    "GovernanceVerdict",
    "BasePlatformAdapter",
    "K8sPlatformAdapter",
    "AWSPlatformAdapter",
    "PlatformRegistry",
    "get_platform_registry",
    "TargetResource",
    "PlatformTelemetry",
    "LiveStateSnapshot",
    "RollbackSnapshot",
    "HealthCheckResult",
    "GraphCheckpointStore",
]


def __getattr__(name: str) -> Any:
    if name in ("IncidentWorkflow", "build_incident_graph"):
        from nexus.engine.workflow.graph.workflow import (
            IncidentWorkflow,
            build_incident_graph,
        )

        if name == "IncidentWorkflow":
            return IncidentWorkflow
        return build_incident_graph
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
