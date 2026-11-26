"""
NEXUS Developer Dashboard API
===============================
Three developer-facing views backed by existing NEXUS data stores.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from nexus.core.database.connection.client import get_database_client
from nexus.services.gateway.auth.operator_auth import require_operator
from nexus.services.observability.dashboard.dashboard_incidents import (
    _in_memory_incidents,
    determine_active_graph_node,
    make_plain_english,
    read_agent_messages,
    read_agent_runs,
    read_incidents,
    record_in_memory_incident,
    record_incident_resolution,
    serialize_incident_for_dashboard,
    write_incident,
)
from nexus.services.observability.dashboard.dashboard_policy import (
    PolicyOverride,
    _policy_cache,
    _policy_overrides,
    apply_policy_override,
    cache_policy,
    clear_policy_overrides,
    get_resolved_policy,
    refresh_policy_cache,
)
from nexus.services.observability.dashboard.dashboard_predictions import (
    get_predictions,
    make_prediction_sentence,
)
from nexus.services.observability.dashboard.dashboard_queries import (
    accept_incident_claim,
    fetch_developer_incidents,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Re-exports for backwards compatibility
_record_in_memory_incident = record_in_memory_incident
_write_incident = write_incident
_read_incidents = read_incidents
_make_plain_english = make_plain_english
_make_prediction_sentence = make_prediction_sentence

__all__ = [
    "router",
    "cache_policy",
    "refresh_policy_cache",
    "record_incident_resolution",
    "determine_active_graph_node",
    "serialize_incident_for_dashboard",
    "get_database_client",
    "PolicyOverride",
    "_policy_cache",
    "_policy_overrides",
    "_in_memory_incidents",
    "_write_incident",
    "_read_incidents",
    "_make_plain_english",
    "_make_prediction_sentence",
]


def _context():
    from nexus.services.api.status.status_api import context

    return context


@router.get(
    "/developer/incidents", tags=["developer"], dependencies=[Depends(require_operator)]
)
async def developer_incidents(
    n: int = 20,
    app: str | None = None,
) -> list[dict[str, Any]]:
    """Plain-English healing incident feed."""
    return await fetch_developer_incidents(
        n=n,
        app=app,
        ctx=_context(),
        read_incidents_fn=read_incidents,
        in_memory_incidents=_in_memory_incidents,
        make_plain_english_fn=_make_plain_english,
    )


@router.get(
    "/developer/incidents/{incident_id}",
    tags=["developer"],
    dependencies=[Depends(require_operator)],
)
async def developer_incident(
    incident_id: str,
    app: str | None = None,
) -> dict[str, Any]:
    """Single incident detail, merged from the DB, in-memory buffer and LangGraph checkpoint."""
    records = await fetch_developer_incidents(
        n=1,
        app=app,
        ctx=_context(),
        read_incidents_fn=read_incidents,
        in_memory_incidents=_in_memory_incidents,
        make_plain_english_fn=_make_plain_english,
        incident_id=incident_id,
    )
    if not records:
        raise HTTPException(
            status_code=404,
            detail=f"No incident found with id '{incident_id}'.",
        )
    return records[0]


@router.post(
    "/developer/incidents", tags=["developer"], dependencies=[Depends(require_operator)]
)
async def developer_post_incident(payload: dict[str, Any]) -> dict[str, str]:
    """Accept an incident directly from demo orchestrator or external caller."""
    payload.setdefault("incident_id", str(uuid.uuid4())[:8])
    payload.setdefault(
        "timestamp",
        datetime.now(timezone.utc).isoformat()[:19].replace("T", " "),
    )
    if not payload.get("description") and payload.get("runbook_id"):
        payload["description"] = _make_plain_english(payload)

    await _write_incident(payload)
    logger.info(
        f"[Dashboard] Incident stored: {payload.get('incident_id')} outcome={payload.get('outcome')}"
    )
    return {"status": "ok", "incident_id": payload["incident_id"]}


@router.post(
    "/developer/incidents/{incident_id}/accept",
    tags=["developer"],
    dependencies=[Depends(require_operator)],
)
async def developer_accept_incident(
    incident_id: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Claim ownership of an incident from the developer dashboard."""
    import nexus.services.observability.dashboard.dashboard as dash_mod

    return await accept_incident_claim(
        incident_id=incident_id,
        payload=payload,
        in_memory_incidents=_in_memory_incidents,
        get_db_fn=dash_mod.get_database_client,
    )


@router.get(
    "/developer/incidents/{incident_id}/agent-runs",
    tags=["developer"],
    dependencies=[Depends(require_operator)],
)
async def developer_incident_agent_runs(incident_id: str) -> list[dict[str, Any]]:
    """Agent runs recorded for one incident."""
    return await read_agent_runs(incident_id)


@router.get(
    "/developer/incidents/{incident_id}/messages",
    tags=["developer"],
    dependencies=[Depends(require_operator)],
)
async def developer_incident_messages(incident_id: str) -> list[dict[str, Any]]:
    """Agent messages recorded for one incident."""
    return await read_agent_messages(incident_id)


# Deliberately NOT behind `require_operator`: report_url() embeds this URL in
# Slack post-incident cards for a human to click, and a browser cannot send a
# bearer token. It is read-only, and its contents are already partly disclosed
# by the Slack notification that carries the link. Signed, expiring report URLs
# would be the way to close this properly.
@router.get("/developer/incidents/{incident_id}/report", tags=["developer"])
async def developer_incident_report(incident_id: str, request: Request) -> Response:
    """Full post-incident report for one incident."""
    from nexus.services.reporting.incident_report import (
        get_incident_report,
        render_report_html,
        render_report_missing_html,
    )

    wants_html = "text/html" in request.headers.get("accept", "")
    report = await get_incident_report(incident_id)
    if report is None:
        if wants_html:
            return HTMLResponse(
                status_code=404, content=render_report_missing_html(incident_id)
            )
        raise HTTPException(
            status_code=404,
            detail=f"No post-incident report found for incident '{incident_id}'.",
        )
    if wants_html:
        return HTMLResponse(content=render_report_html(report))
    return JSONResponse(content=report)


@router.get(
    "/developer/predictions",
    tags=["developer"],
    dependencies=[Depends(require_operator)],
)
def developer_predictions(app: str | None = None) -> list[dict[str, Any]]:
    """Current traffic predictions from the Prescaler."""
    return get_predictions(_context().prescaler, app=app)


@router.get(
    "/developer/policy/{app}",
    tags=["developer"],
    dependencies=[Depends(require_operator)],
)
def developer_get_policy(app: str) -> dict[str, Any]:
    """Return the resolved selfheal.yaml policy for an app."""
    return get_resolved_policy(app)


@router.post(
    "/developer/policy/{app}",
    tags=["developer"],
    dependencies=[Depends(require_operator)],
)
def developer_upload_policy(app: str, policy: dict[str, Any]) -> dict[str, Any]:
    """Upload and cache a selfheal.yaml policy for an app."""
    cache_policy(app, policy)
    return {
        "app": app,
        "message": "Policy uploaded and cached.",
        "fields": list(policy.keys()),
    }


@router.put(
    "/developer/policy/{app}",
    tags=["developer"],
    dependencies=[Depends(require_operator)],
)
def developer_put_policy(app: str, override: PolicyOverride) -> dict[str, Any]:
    """Apply a runtime override to an app's selfheal.yaml policy."""
    return apply_policy_override(app, override)


@router.delete(
    "/developer/policy/{app}/overrides",
    tags=["developer"],
    dependencies=[Depends(require_operator)],
)
def developer_clear_overrides(app: str) -> dict[str, str]:
    """Clear all runtime overrides for an app."""
    return clear_policy_overrides(app)
