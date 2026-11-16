"""Incident database operations and formatting for the developer dashboard."""

from __future__ import annotations

import json as _json
import logging
from datetime import datetime, timezone
from typing import Any

from nexus.db.postgres import PostgresClient

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
