"""
Initial graph state builder and discovered-identity DB sync helper for NEXUS workflows.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from nexus.engine.workflow.state.state import IncidentState

logger = logging.getLogger(__name__)


def build_initial_state(incident_data: dict[str, Any], inc_id: str) -> IncidentState:
    """
    Construct initial IncidentState without fabricating 'default' or 'unknown-service'.
    Unknown identity fields remain None so diagnose_node discovers true topology via tools.
    """
    raw_events = list(
        incident_data.get("raw_events") or incident_data.get("events") or []
    )
    if not raw_events and "event" in incident_data and incident_data["event"]:
        raw_events = [incident_data["event"]]

    platform: str | None = incident_data.get("platform")
    namespace: str | None = incident_data.get("namespace")
    resource_kind: str | None = incident_data.get("resource_kind")
    service_name: str | None = incident_data.get("service_name")
    target_resource: str | None = incident_data.get("target_resource")

    # Extract explicit non-fabricated hints from raw_events if not passed top-level
    if raw_events and isinstance(raw_events[0], dict):
        first = raw_events[0]
        if platform is None:
            raw_plat = (
                first.get("platform") or first.get("provider") or first.get("agent")
            )
            if raw_plat:
                p_low = str(raw_plat).lower()
                platform = "k8s" if p_low in ("k8s", "kubernetes") else p_low
        if namespace is None and first.get("namespace"):
            namespace = first["namespace"]
        if service_name is None and first.get("service_name"):
            service_name = first["service_name"]
        if resource_kind is None and first.get("resource_kind"):
            resource_kind = first["resource_kind"]

    state: IncidentState = {
        "incident_id": inc_id,
        "platform": platform,
        "namespace": namespace,
        "resource_kind": resource_kind,
        "service_name": service_name,
        "target_resource": target_resource,
        "raw_events": raw_events,
        "severity": str(incident_data.get("severity") or "warning"),
        "triage_status": str(incident_data.get("triage_status") or "PENDING"),
        "min_confidence_threshold": float(
            incident_data.get("min_confidence_threshold", 0.65)
        ),
        "max_diagnose_steps": int(incident_data.get("max_diagnose_steps", 6)),
        "execution_results": list(incident_data.get("execution_results") or []),
        "remediation_attempts": int(incident_data.get("remediation_attempts", 0)),
        "max_remediation_attempts": int(
            incident_data.get("max_remediation_attempts", 2)
        ),
        "rollback_available": bool(incident_data.get("rollback_available", False)),
        "rollback_results": list(incident_data.get("rollback_results") or []),
        "status": str(incident_data.get("status") or "DETECTED"),
    }

    if "approved" in incident_data:
        state["approved"] = incident_data["approved"]
    if "approval_status" in incident_data:
        state["approval_status"] = incident_data["approval_status"]

    return state


async def sync_discovered_target_identity(
    incident_id: str, state: Mapping[str, Any]
) -> None:
    """
    Persist dynamically discovered namespace, service_name, target_resource,
    and failure_class to PostgreSQL so DB records and Slack/dashboard views
    always reflect the authoritative diagnosed topology before approval or escalation.
    """
    target_resource = state.get("target_resource")
    namespace = state.get("namespace")
    resource_kind = state.get("resource_kind")
    service_name = state.get("service_name")
    failure_class = state.get("failure_class")

    if not target_resource and namespace and service_name:
        kind = resource_kind or "Resource"
        target_resource = f"{namespace}/{kind}/{service_name}"

    if not target_resource:
        return

    try:
        db_client = None
        try:
            from nexus.services.api.status.status_api import context

            db_client = getattr(context, "db_client", None)
        except Exception:
            pass

        if db_client is None:
            from nexus.core.database.connection.client import _global_db_client

            db_client = _global_db_client

        if db_client is not None and hasattr(db_client, "update_discovered_target"):
            await db_client.update_discovered_target(
                incident_id,
                target_resource=target_resource,
                namespace=namespace,
                resource_kind=resource_kind,
                service_name=service_name,
                failure_class=failure_class,
                root_cause_summary=state.get("suspected_cause"),
                confidence=state.get("confidence"),
            )
    except Exception as exc:
        logger.debug(
            "[WorkflowState] Non-fatal DB discovered target sync skipped for %s: %s",
            incident_id,
            exc,
        )
