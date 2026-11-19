"""
NEXUS Closed-Loop Learning Node
===============================
Terminal learning stage: marks the incident RESOLVED and records audit/outcome metadata
using the discovered workload identity and open failure_class.
"""

from __future__ import annotations

import logging
from typing import Any

from nexus.engine.workflow.state.state import IncidentState
from nexus.engine.workflow.state.workflow_state import (
    sync_discovered_target_identity,
)

logger = logging.getLogger(__name__)


async def learn_node(state: IncidentState) -> dict[str, Any]:
    """Record resolved incident into the learning plane and mark terminal status."""
    incident_id = state.get("incident_id", "unknown")
    target_resource = state.get("target_resource") or (
        f"{state.get('namespace')}/{state.get('resource_kind')}/{state.get('service_name')}"
        if state.get("namespace") and state.get("service_name")
        else state.get("service_name")
    )
    failure_class = state.get("failure_class", "unclassified")
    platform = state.get("platform") or "k8s"

    await sync_discovered_target_identity(incident_id, state)

    try:
        from nexus.services.api.status.status_api import context

        if getattr(context, "audit_trail", None) is not None:
            await context.audit_trail.write(
                triggered_by=f"nexus:workflow:{platform}",
                runbook_id=str(failure_class),
                healing_level=1,
                target=str(target_resource or incident_id),
                pre_check_results={"rca_result": state.get("rca_result")},
                execution_outcome="success",
                post_check_results={
                    "verification_passed": state.get("verification_passed"),
                    "verification_feedback": state.get("verification_feedback"),
                },
                incident_id=incident_id,
                action_results=state.get("execution_results", []),
            )
    except Exception as audit_err:
        logger.debug("[Learn] AuditTrail write skipped: %s", audit_err)

    logger.info(
        "Incident %s recorded into learning plane: target=%s failure_class=%s",
        incident_id,
        target_resource,
        failure_class,
    )

    return {
        "status": "RESOLVED",
    }
