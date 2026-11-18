"""Incident database operations and formatting for the developer dashboard."""

from __future__ import annotations

import json as _json
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from nexus.db.client import PostgresClient

logger = logging.getLogger(__name__)

_in_memory_incidents: list[dict[str, Any]] = []
_RUNBOOK_DESCRIPTIONS: dict[str, str] = {}

_OUTCOME_SUFFIX = {
    "success": "Successfully resolved.",
    "failed": "Healing attempt failed — may need manual review.",
    "rolled_back": "Action was rolled back (post-check failed).",
    "pending": "Action is in progress.",
}


def make_plain_english(row: dict[str, Any]) -> str:
    """Turn an AuditTrail row into a plain-English incident description."""
    action = (
        row.get("description") or row.get("action_taken") or row.get("runbook_id", "")
    )
    target = row.get("target") or row.get("resource_name") or "the service"
    outcome = row.get("execution_outcome", "pending")
    ts = (row.get("timestamp") or "")[:19].replace("T", " ")
    action_text = (
        action
        if " on " in str(action)
        else (
            f"Remediation '{action}' on {target}"
            if action
            else f"Remediation on {target}"
        )
    )
    return f"At {ts} UTC — {action_text}. {_OUTCOME_SUFFIX.get(outcome, '')}".strip()


def record_in_memory_incident(row: dict[str, Any]) -> None:
    """Store incident in memory buffer (capped at 200)."""
    inc_id = row.get("incident_id")
    if inc_id:
        for idx, item in enumerate(_in_memory_incidents):
            if item.get("incident_id") == inc_id:
                _in_memory_incidents[idx] = {**item, **row}
                return
    _in_memory_incidents.append(dict(row))
    if len(_in_memory_incidents) > 200:
        _in_memory_incidents.pop(0)


async def write_incident(
    row: dict[str, Any], client: PostgresClient | None = None
) -> None:
    """Write one incident to PostgreSQL developer_incidents table and local buffer."""
    import nexus.integration.dashboard as dash_mod

    record_in_memory_incident(row)
    rca_val = row.get("rca")
    if isinstance(rca_val, (dict, list)):
        rca_val = _json.dumps(rca_val)
    elif rca_val is not None:
        rca_val = str(rca_val)

    try:
        db = client or await dash_mod.get_database_client()
        sql = (
            "INSERT INTO developer_incidents (incident_id, runbook_id, target, level, outcome, "
            "description, confidence, timestamp, rca, accepted_by, accepted_at, diagnostic_logs, remediation_yaml) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12, $13)"
        )
        conf = (
            float(row.get("confidence", 0.0))
            if row.get("confidence") is not None
            else None
        )
        async with db.acquire() as conn:
            await conn.execute(
                "ALTER TABLE developer_incidents ADD COLUMN IF NOT EXISTS diagnostic_logs TEXT; "
                "ALTER TABLE developer_incidents ADD COLUMN IF NOT EXISTS remediation_yaml TEXT;"
            )
            await conn.execute(
                sql,
                row.get("incident_id"),
                row.get("runbook_id"),
                row.get("target"),
                row.get("level"),
                row.get("outcome"),
                row.get("description"),
                conf,
                str(row.get("timestamp") or ""),
                rca_val,
                row.get("accepted_by"),
                row.get("accepted_at"),
                row.get("diagnostic_logs"),
                row.get("remediation_yaml"),
            )
            await conn.execute(
                "DELETE FROM developer_incidents WHERE id NOT IN (SELECT id FROM developer_incidents ORDER BY id DESC LIMIT 200)"
            )
        logger.info(
            f"[Dashboard] Incident stored: {row.get('incident_id')} outcome={row.get('outcome')}"
        )
    except Exception as _e:
        logger.warning(f"[Dashboard] incident write failed: {_e}")


async def record_incident_resolution(
    data: dict[str, Any], client: PostgresClient | None = None
) -> None:
    """Format and record an incident resolution into developer_incidents table."""
    import nexus.integration.dashboard as dash_mod

    inc_id = data.get("incident_id")
    target = (
        data.get("target") or data.get("target_name") or data.get("app") or "service"
    )
    plan = data.get("plan") or {}
    rca = data.get("rca") or data.get("diagnosis") or {}
    runbook_id = (
        plan.get("failure_mode") or data.get("runbook_id") or "dynamic_remediation"
    )
    level = data.get("level", 3)
    outcome = data.get("outcome", "success")

    failure_class = rca.get("failure_class") or "issue"
    steps = plan.get("steps") or plan.get("actions") or []
    action_desc = (
        steps[0].get("description")
        if steps and steps[0].get("description")
        else data.get("action_taken")
        or data.get("description")
        or f"Executed remediation for {failure_class}"
    )

    ts = (
        data.get("resolved_at")
        or data.get("timestamp")
        or datetime.now(timezone.utc).isoformat()
    )[:19].replace("T", " ")
    desc = (
        f"At {ts} UTC — {action_desc} on {target}. Successfully resolved and verified healthy."
        if outcome == "success"
        else f"At {ts} UTC — Incident on {target} escalated: {data.get('reason', 'Failed')}"
    )

    confidence = data.get("confidence")
    if confidence is None and rca.get("confidence") is not None:
        confidence = rca.get("confidence")

    rem_yaml = data.get("remediation_yaml")
    if not rem_yaml and plan:
        from nexus.graph.manifest_formatter import plan_to_yaml_manifest

        rem_yaml = plan_to_yaml_manifest(plan, target=str(target))

    diag_logs = data.get("diagnostic_logs")
    if not diag_logs:
        from nexus.graph.manifest_formatter import extract_diagnostic_logs

        diag_logs = extract_diagnostic_logs(data)

    row = {
        "incident_id": inc_id,
        "runbook_id": runbook_id,
        "target": target,
        "level": level,
        "outcome": outcome,
        "description": desc,
        "confidence": float(confidence) if confidence is not None else 1.0,
        "timestamp": ts,
        "rca": rca,
        "accepted_by": data.get("accepted_by"),
        "accepted_at": data.get("accepted_at"),
        "diagnostic_logs": diag_logs,
        "remediation_yaml": rem_yaml,
    }
    await dash_mod._write_incident(row, client=client)


async def read_incidents(
    n: int, app: str | None = None, client: PostgresClient | None = None
) -> list[dict[str, Any]]:
    """Read recent incidents from PostgreSQL developer_incidents table."""
    import nexus.integration.dashboard as dash_mod

    try:
        db = client or await dash_mod.get_database_client()
        sql = "SELECT * FROM developer_incidents ORDER BY id DESC LIMIT $1"
        async with db.acquire() as conn:
            rows = await conn.fetch(sql, max(n, 200))
        results = []
        for r in rows:
            d = dict(r)
            if app and app.lower() not in (d.get("target") or "").lower():
                continue
            if d.get("rca") and isinstance(d["rca"], str):
                try:
                    d["rca"] = _json.loads(d["rca"])
                except Exception:
                    pass
            results.append(d)
            if len(results) >= n:
                break
        return results
    except Exception as _e:
        logger.warning(f"[Dashboard] incident read failed: {_e}")
        return []


def _to_iso_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _extract_event_hint(raw_events: Sequence[Any], *keys: str) -> str | None:
    for ev in raw_events:
        if not isinstance(ev, Mapping):
            continue
        for container in (
            ev,
            ev.get("raw_payload"),
            ev.get("context"),
            ev.get("raw"),
        ):
            if not isinstance(container, Mapping):
                continue
            for key in keys:
                val = container.get(key)
                if val:
                    return str(val)
    return None


def determine_active_graph_node(
    state: Mapping[str, Any], next_nodes: Sequence[str] | None = None
) -> str:
    """Determines the active or terminal LangGraph node for the UI stepper."""
    if next_nodes:
        first_next = str(next_nodes[0]).strip()
        if first_next:
            return first_next.upper()

    status = str(state.get("status") or "").upper()
    triage = str(state.get("triage_status") or "").upper()
    approval_status = str(state.get("approval_status") or "").upper()

    if triage == "IGNORED" or status == "IGNORED":
        return "TRIAGE"
    if (
        status in ("AWAITING_APPROVAL", "PENDING_APPROVAL")
        or approval_status == "PENDING"
    ):
        return "APPROVAL"
    if status == "DIAGNOSED":
        return "DIAGNOSE"
    if status in ("PLANNED", "UNPLANNABLE"):
        return "PLAN"
    if status in ("AUTO_APPROVED", "APPROVED", "POLICY_DENIED"):
        return "GOVERN"
    if status in ("EXECUTED", "EXECUTION_FAILED", "EXECUTING"):
        return "EXECUTE"
    if status == "VERIFICATION_FAILED":
        return "VERIFY"
    if status == "ROLLED_BACK":
        return "ROLLBACK"
    if status == "RESOLVED":
        return "LEARN"
    if status == "ESCALATED":
        return "ESCALATE"

    # Legacy outcome fallback
    outcome = str(state.get("outcome") or "").lower()
    if outcome == "success":
        return "LEARN"
    if outcome == "rolled_back":
        return "ROLLBACK"
    if outcome == "failed":
        return "ESCALATE"
    return "DIAGNOSE"


def _derive_legacy_status(record: Mapping[str, Any]) -> str:
    explicit = record.get("status")
    if explicit:
        return str(explicit).upper()
    outcome = str(record.get("outcome") or "").lower()
    if outcome == "success":
        return "RESOLVED"
    if outcome == "rolled_back":
        return "ROLLED_BACK"
    if outcome == "failed":
        return "ESCALATED"
    return "INVESTIGATING"


def _derive_legacy_outcome(status: str, existing_outcome: Any) -> str:
    if existing_outcome:
        return str(existing_outcome)
    upper = status.upper()
    if upper == "RESOLVED":
        return "success"
    if upper == "ROLLED_BACK":
        return "rolled_back"
    if upper in ("ESCALATED", "EXECUTION_FAILED", "POLICY_DENIED"):
        return "failed"
    return "pending"


def serialize_incident_for_dashboard(
    incident_id: str,
    state: Mapping[str, Any],
    next_nodes: Sequence[str] | None = None,
    updated_at: str | None = None,
    legacy_record: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Serializes a LangGraph IncidentState + checkpoint + DB metadata into the Developer Dashboard schema."""
    legacy = dict(legacy_record or {})
    trigger_payload = (
        legacy.get("trigger_payload")
        if isinstance(legacy.get("trigger_payload"), Mapping)
        else {}
    )

    rca_raw = state.get("rca_result")
    if not isinstance(rca_raw, Mapping):
        rca_raw = legacy.get("rca") if isinstance(legacy.get("rca"), Mapping) else {}
    rca: dict[str, Any] = dict(rca_raw)
    rca_target = rca.get("target") if isinstance(rca.get("target"), Mapping) else {}

    plan_raw = state.get("remediation_plan")
    if not isinstance(plan_raw, Mapping):
        plan_raw = legacy.get("plan") if isinstance(legacy.get("plan"), Mapping) else {}
    plan: dict[str, Any] = dict(plan_raw)
    steps: list[dict[str, Any]] = list(plan.get("steps") or [])

    policy_raw = state.get("policy_decision")
    policy: dict[str, Any] = dict(policy_raw) if isinstance(policy_raw, Mapping) else {}

    exec_results: list[dict[str, Any]] = list(
        state.get("execution_results") or legacy.get("execution_results") or []
    )
    raw_events: list[Any] = list(state.get("raw_events") or [])

    involved_obj = _extract_event_hint(
        raw_events, "involved_object_name", "pod_name", "resource_name"
    ) or legacy.get("involved_object_name")
    involved_kind = (
        _extract_event_hint(raw_events, "involved_object_kind")
        or legacy.get("involved_object_kind")
        or "Pod"
    )

    namespace = (
        state.get("namespace")
        or rca_target.get("namespace_or_region")
        or trigger_payload.get("discovered_namespace")
        or legacy.get("namespace")
    )
    resource_kind = (
        state.get("resource_kind")
        or rca_target.get("resource_kind")
        or trigger_payload.get("discovered_resource_kind")
        or legacy.get("resource_kind")
        or "Workload"
    )
    service_name = (
        state.get("service_name")
        or rca_target.get("resource_name")
        or trigger_payload.get("discovered_service_name")
        or legacy.get("service_name")
    )
    legacy_target = legacy.get("target_resource") or legacy.get("target")
    target_resource = (
        state.get("target_resource")
        or trigger_payload.get("discovered_target_resource")
        or (
            f"{namespace}/{resource_kind}/{service_name}"
            if namespace and service_name
            else (str(legacy_target) if legacy_target else "Discovering via ReAct Tools...")
        )
    )

    merged_for_node: dict[str, Any] = {**legacy, **dict(state)}
    raw_status = str(state.get("status") or _derive_legacy_status(legacy)).upper()
    next_list = [str(n) for n in (next_nodes or ())]
    active_node = determine_active_graph_node(merged_for_node, next_list)

    approval_status = str(
        state.get("approval_status") or legacy.get("approval_status") or ""
    ).upper()
    is_awaiting_approval = bool(
        (next_list and active_node == "APPROVAL")
        or raw_status in ("AWAITING_APPROVAL", "PENDING_APPROVAL")
        or approval_status == "PENDING"
    )
    normalized_status = "AWAITING_APPROVAL" if is_awaiting_approval else raw_status

    raw_confidence = (
        state.get("confidence")
        if state.get("confidence") is not None
        else (
            rca.get("confidence")
            if rca.get("confidence") is not None
            else (
                legacy.get("confidence")
                if legacy.get("confidence") is not None
                else legacy.get("confidence_score")
            )
        )
    )
    confidence = float(raw_confidence) if raw_confidence is not None else 0.0
    min_confidence_threshold = float(state.get("min_confidence_threshold") or 0.65)

    failure_class = (
        state.get("failure_class")
        or rca.get("failure_type")
        or rca.get("failure_class")
        or trigger_payload.get("failure_class")
        or legacy.get("failure_class")
        or legacy.get("runbook_id")
        or "investigating"
    )
    suspected_cause = (
        state.get("suspected_cause")
        or rca.get("root_cause_summary")
        or rca.get("root_cause")
        or legacy.get("root_cause_summary")
        or legacy.get("description")
        or ""
    )

    resolved_updated_at = (
        _to_iso_timestamp(updated_at)
        or _to_iso_timestamp(state.get("updated_at"))
        or _to_iso_timestamp(legacy.get("updated_at"))
        or _to_iso_timestamp(legacy.get("timestamp"))
        or _to_iso_timestamp(legacy.get("created_at"))
    )

    requires_approval = bool(
        policy.get(
            "requires_approval",
            state.get("requires_approval", is_awaiting_approval),
        )
    )
    policy_decision = {
        "allowed": bool(policy.get("allowed", normalized_status != "POLICY_DENIED")),
        "requires_approval": requires_approval,
        "reason": policy.get("reason") or "Evaluated against OPA attribute policy",
    }

    resolved_approval_status = approval_status or (
        "PENDING" if is_awaiting_approval else "N/A"
    )
    approved = state.get("approved")
    approved_by = state.get("approved_by") or legacy.get("approved_by")
    approval_reason = state.get("approval_reason") or legacy.get("approval_reason")

    execution_status = str(
        state.get("execution_status")
        or (
            "FAILED"
            if normalized_status == "EXECUTION_FAILED"
            else ("SUCCESS" if exec_results else "PENDING")
        )
    )
    remediation_attempts = int(state.get("remediation_attempts") or 0)
    max_remediation_attempts = int(state.get("max_remediation_attempts") or 2)
    verification_passed = state.get("verification_passed")
    verification_feedback = str(state.get("verification_feedback") or "")
    rollback_available = bool(state.get("rollback_available", False))
    rollback_results: list[dict[str, Any]] = list(state.get("rollback_results") or [])
    post_incident_report_ready = len(exec_results) > 0 and normalized_status in (
        "RESOLVED",
        "ROLLED_BACK",
    )

    platform = (
        state.get("platform")
        or rca_target.get("platform")
        or legacy.get("platform")
        or legacy.get("environment")
        or "k8s"
    )
    if str(platform).lower() == "kubernetes":
        platform = "k8s"

    payload: dict[str, Any] = {
        "incident_id": incident_id,
        "updated_at": resolved_updated_at,
        "platform": str(platform),
        "namespace": namespace,
        "resource_kind": resource_kind,
        "service_name": service_name,
        "target_resource": target_resource,
        "involved_object": f"{involved_kind}/{involved_obj}" if involved_obj else None,
        "involved_object_name": involved_obj,
        "severity": str(state.get("severity") or legacy.get("severity") or "WARNING").upper(),
        "status": normalized_status,
        "active_node": active_node,
        "next_nodes": next_list,
        "triage_status": str(state.get("triage_status") or "TRIAGED"),
        "failure_class": str(failure_class),
        "suspected_cause": str(suspected_cause),
        "confidence": confidence,
        "min_confidence_threshold": min_confidence_threshold,
        "can_auto_remediate": bool(rca.get("can_auto_remediate", True)),
        "recommended_strategy": str(
            rca.get("recommended_strategy") or plan.get("rationale") or ""
        ),
        "evidence": list(rca.get("evidence") or []),
        "rca_result": rca,
        "remediation_plan": {
            "rationale": str(plan.get("rationale") or ""),
            "steps": steps,
        },
        "policy_decision": policy_decision,
        "requires_approval": requires_approval,
        "approval_status": resolved_approval_status,
        "approved": approved,
        "approved_by": approved_by,
        "approval_reason": approval_reason,
        "is_interrupted": is_awaiting_approval,
        "approval": {
            "requires_approval": requires_approval,
            "approval_status": resolved_approval_status,
            "approved": approved,
            "approved_by": approved_by,
            "approval_reason": approval_reason,
            "is_interrupted": is_awaiting_approval,
        },
        "execution_status": execution_status,
        "execution_results": exec_results,
        "remediation_attempts": remediation_attempts,
        "max_remediation_attempts": max_remediation_attempts,
        "execution": {
            "execution_status": execution_status,
            "results": exec_results,
            "remediation_attempts": remediation_attempts,
            "max_remediation_attempts": max_remediation_attempts,
        },
        "verification_passed": verification_passed,
        "verification_feedback": verification_feedback,
        "verification": {
            "verification_passed": verification_passed,
            "verification_feedback": verification_feedback,
        },
        "rollback_available": rollback_available,
        "rollback_results": rollback_results,
        "rollback": {
            "rollback_available": rollback_available,
            "rollback_results": rollback_results,
        },
        "error": state.get("error") or legacy.get("error"),
        "post_incident_report_ready": post_incident_report_ready,
        # Legacy fields preserved for backwards-compatible API consumers & unit tests
        "target": legacy.get("target") or target_resource,
        "outcome": _derive_legacy_outcome(normalized_status, legacy.get("outcome")),
        "timestamp": legacy.get("timestamp") or resolved_updated_at or "",
    }

    for legacy_key in (
        "level",
        "description",
        "runbook_id",
        "accepted_by",
        "accepted_at",
        "rca",
        "diagnostic_logs",
        "remediation_yaml",
    ):
        if legacy_key in legacy and legacy[legacy_key] is not None:
            payload[legacy_key] = legacy[legacy_key]

    if "rca" not in payload and rca:
        payload["rca"] = rca

    return payload
