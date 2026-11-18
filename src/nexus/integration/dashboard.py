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

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from nexus.db.client import get_database_client
from nexus.integration.dashboard_incidents import (
    _in_memory_incidents,
    determine_active_graph_node,
    make_plain_english,
    read_incidents,
    record_in_memory_incident,
    record_incident_resolution,
    serialize_incident_for_dashboard,
    write_incident,
)
from nexus.integration.dashboard_policy import (
    PolicyOverride,
    _policy_cache,
    _policy_overrides,
    apply_policy_override,
    cache_policy,
    clear_policy_overrides,
    get_resolved_policy,
    refresh_policy_cache,
)
from nexus.integration.dashboard_predictions import (
    get_predictions,
    make_prediction_sentence,
)
from nexus.integration.dashboard_queries import (
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
    from nexus.observability.status_api import context

    return context


@router.get("/developer/incidents", tags=["developer"])
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


@router.post("/developer/incidents", tags=["developer"])
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


@router.post("/developer/incidents/{incident_id}/accept", tags=["developer"])
async def developer_accept_incident(
    incident_id: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Claim ownership of an incident from the developer dashboard."""
    import nexus.integration.dashboard as dash_mod

    return await accept_incident_claim(
        incident_id=incident_id,
        payload=payload,
        in_memory_incidents=_in_memory_incidents,
        get_db_fn=dash_mod.get_database_client,
    )


@router.get("/developer/incidents/{incident_id}/report", tags=["developer"])
async def developer_incident_report(incident_id: str, request: Request) -> Response:
    """Full post-incident report for one incident."""
    from nexus.integration.incident_report import (
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


@router.get("/developer/predictions", tags=["developer"])
def developer_predictions(app: str | None = None) -> list[dict[str, Any]]:
    """Current traffic predictions from the Prescaler."""
    return get_predictions(_context().prescaler, app=app)


@router.get("/developer/policy/{app}", tags=["developer"])
def developer_get_policy(app: str) -> dict[str, Any]:
    """Return the resolved selfheal.yaml policy for an app."""
    return get_resolved_policy(app)


@router.post("/developer/policy/{app}", tags=["developer"])
def developer_upload_policy(app: str, policy: dict[str, Any]) -> dict[str, Any]:
    """Upload and cache a selfheal.yaml policy for an app."""
    cache_policy(app, policy)
    return {
        "app": app,
        "message": "Policy uploaded and cached.",
        "fields": list(policy.keys()),
    }


@router.put("/developer/policy/{app}", tags=["developer"])
def developer_put_policy(app: str, override: PolicyOverride) -> dict[str, Any]:
    """Apply a runtime override to an app's selfheal.yaml policy."""
    return apply_policy_override(app, override)


@router.delete("/developer/policy/{app}/overrides", tags=["developer"])
def developer_clear_overrides(app: str) -> dict[str, str]:
    """Clear all runtime overrides for an app."""
    return clear_policy_overrides(app)
