"""
Aggregated incident queries and claim management for the developer dashboard.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


async def fetch_developer_incidents(
    n: int,
    app: str | None,
    ctx: Any,
    read_incidents_fn: Any,
    in_memory_incidents: list[dict[str, Any]],
    make_plain_english_fn: Any,
) -> list[dict[str, Any]]:
    """Fetch, merge, and enrich healing incidents."""
    raw_results = await read_incidents_fn(n=n, app=app)
    results: list[dict[str, Any]] = (
        list(raw_results) if isinstance(raw_results, (list, tuple)) else []
    )
    seen_ids = {r.get("incident_id") for r in results if r.get("incident_id")}

    for entry in reversed(in_memory_incidents):
        inc_id = entry.get("incident_id")
        if inc_id and inc_id in seen_ids:
            continue
        if app:
            target = (entry.get("target") or "").lower()
            if app.lower() not in target:
                continue
        results.append(dict(entry))
        if inc_id:
            seen_ids.add(inc_id)
        if len(results) >= n:
            break

    if ctx.audit_trail is not None and len(results) < n:
        try:
            rows = await ctx.audit_trail.query_recent(limit=max(n * 3, 60))
            for row in rows:
                inc_id = row.get("incident_id") or row.get("correlation_id")
                if inc_id and inc_id in seen_ids:
                    continue
                if app:
                    target = (
                        row.get("target") or row.get("resource_name") or ""
                    ).lower()
                    if app.lower() not in target:
                        continue
                    results.append(
                        {
                            "timestamp": (row.get("timestamp") or "")[:19],
                            "runbook_id": row.get("runbook_id"),
                            "target": row.get("target") or row.get("resource_name"),
                            "level": row.get("healing_level"),
                            "outcome": row.get("execution_outcome", "pending"),
                            "description": make_plain_english_fn(row),
                            "incident_id": inc_id,
                        }
                    )
                    if inc_id:
                        seen_ids.add(inc_id)
                    if len(results) >= n:
                        break
        except Exception as _e:
            logger.debug(f"[Dashboard] AuditTrail read skipped: {_e}")

    if ctx.orchestrator is not None:
        try:
            recent_rca = ctx.orchestrator.last_rca_results(n=50)
            rca_by_inc = {
                r.get("incident_id"): r.get("rca")
                for r in recent_rca
                if r.get("incident_id") and r.get("rca")
            }
            rca_by_target = {
                r.get("validation", {}).get("target", ""): r.get("rca")
                for r in recent_rca
                if r.get("validation", {}).get("target") and r.get("rca")
            }
            for item in results:
                if not item.get("rca"):
                    inc_id = item.get("incident_id")
                    target = item.get("target")
                    if inc_id in rca_by_inc:
                        item["rca"] = rca_by_inc[inc_id]
                    elif target and target in rca_by_target:
                        item["rca"] = rca_by_target[target]
        except Exception as _e:
            logger.debug(f"[Dashboard] RCA enrichment skipped: {_e}")

    results.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return results[:n]


async def accept_incident_claim(
    incident_id: str,
    payload: dict[str, Any] | None,
    in_memory_incidents: list[dict[str, Any]],
    get_db_fn: Any,
) -> dict[str, Any]:
    """Claim ownership of an incident from the developer dashboard."""
    user = (payload or {}).get("user", "dev-oncall")
    now_ts = datetime.now(timezone.utc).isoformat()[:19].replace("T", " ")

    for item in in_memory_incidents:
        if item.get("incident_id") == incident_id:
            item["accepted_by"] = user
            item["accepted_at"] = now_ts

    try:
        db = await get_db_fn()
        sql = """
        UPDATE developer_incidents
        SET accepted_by = $1, accepted_at = $2
        WHERE incident_id = $3
        """
        async with db.acquire() as conn:
            await conn.execute(sql, user, now_ts, incident_id)
        logger.info(f"[Dashboard] Incident {incident_id} accepted by {user}")
    except Exception as _e:
        logger.debug(f"[Dashboard] accept DB update skipped: {_e}")

    return {
        "status": "ok",
        "incident_id": incident_id,
        "accepted_by": user,
        "accepted_at": now_ts,
    }
