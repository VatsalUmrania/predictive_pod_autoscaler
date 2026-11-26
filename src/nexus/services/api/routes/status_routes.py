"""
NEXUS Status API System and Observability Routes
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Response

from nexus.services.api.status.status_context import _require, context
from nexus.services.gateway.auth.operator_auth import require_operator
from nexus.services.observability.metrics.metrics import get_metrics

router = APIRouter()

_start_time = time.monotonic()
_DASHBOARD_HTML = Path(__file__).parent / "developer-dashboard.html"


@router.get("/", include_in_schema=False)
def serve_dashboard() -> Response:
    """Serve the NEXUS Developer Dashboard UI at the root URL."""
    if not _DASHBOARD_HTML.exists():
        raise HTTPException(
            status_code=404,
            detail="Dashboard HTML not found. Ensure developer-dashboard.html is bundled with the package.",
        )
    return Response(
        content=_DASHBOARD_HTML.read_text(encoding="utf-8"),
        media_type="text/html",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/health", tags=["system"])
def health() -> dict[str, Any]:
    """Liveness probe — always returns 200 if the server is up."""
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "uptime_seconds": round(time.monotonic() - _start_time, 1),
    }


@router.get("/status", tags=["system"])
async def status() -> dict[str, Any]:
    """Full system snapshot — orchestrator, governance, prescaler, learning."""
    out: dict[str, Any] = {
        "uptime_seconds": round(time.monotonic() - _start_time, 1),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if context.workflow:
        out["workflow"] = context.workflow.status
        out["mode"] = "langgraph_agent"
    elif context.orchestrator:
        out["orchestrator"] = context.orchestrator.status

    if context.prescaler:
        out["prescaler"] = context.prescaler.stats

    if context.feedback_loop:
        out["learning"] = context.feedback_loop.status

    return out


@router.get("/metrics", tags=["observability"], include_in_schema=False)
def prometheus_metrics() -> Response:
    """Expose Prometheus metrics in text format."""
    m = get_metrics()
    return Response(content=m.generate_latest(), media_type=m.content_type)


def _flatten_rca_target(entry: dict[str, Any]) -> dict[str, Any]:
    """Add a flat ``target_resource`` to an RCAResult dump.

    ``RCAResult.target`` is a nested ``DiscoveredTarget`` object, so consumers
    that expect a string target (the dashboard's RCA feed) cannot read it
    directly.
    """
    target = entry.get("target")
    if not isinstance(target, dict):
        return entry
    parts = (
        target.get("namespace_or_region"),
        target.get("resource_kind"),
        target.get("resource_name"),
    )
    flattened = "/".join(str(p) for p in parts if p)
    if flattened:
        entry.setdefault("target_resource", flattened)
    elif target.get("resource_name"):
        entry.setdefault("target_resource", str(target["resource_name"]))
    return entry


@router.get("/rca/last", tags=["reasoning"])
def last_rca(n: int = 10) -> list[dict[str, Any]]:
    """Return the N most recent RCA decisions from the LangGraph workflow."""
    results: list[dict[str, Any]] = []
    if context.workflow and hasattr(context.workflow, "last_rca_results"):
        results = cast(list[dict[str, Any]], context.workflow.last_rca_results(n))
    elif context.orchestrator and hasattr(context.orchestrator, "last_rca_results"):
        results = cast(list[dict[str, Any]], context.orchestrator.last_rca_results(n))
    return [_flatten_rca_target(r) for r in results if isinstance(r, dict)]


@router.post(
    "/prescaler/mode/{mode}",
    tags=["predictive"],
    dependencies=[Depends(require_operator)],
)
def prescaler_set_mode(mode: str) -> dict[str, str]:
    """Change prescaler autonomy mode: shadow | advisory | autonomous."""
    from nexus.intelligence.predictive.scaling.prescaler import PrescaleMode

    p = _require(context.prescaler, "Prescaler")
    if mode not in ("shadow", "advisory", "autonomous"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid mode '{mode}'. Choose: shadow | advisory | autonomous",
        )
    result = p.promote(PrescaleMode(mode))
    return {"status": "ok", "result": result}


@router.get("/learning", tags=["learning"], dependencies=[Depends(require_operator)])
def learning_status() -> dict[str, Any]:
    """FeedbackLoop status and latest system KPIs."""
    fl = _require(context.feedback_loop, "FeedbackLoop")
    return cast(dict[str, Any], fl.status)


@router.get("/api/v1/status/incidents", tags=["observability"])
async def status_incidents(
    n: int = 20,
    app: str | None = None,
) -> list[dict[str, Any]]:
    """Enriched LangGraph incident feed mirroring /developer/incidents."""
    from nexus.services.observability.dashboard.dashboard_incidents import (
        _in_memory_incidents,
        make_plain_english,
        read_incidents,
    )
    from nexus.services.observability.dashboard.dashboard_queries import (
        fetch_developer_incidents,
    )

    return await fetch_developer_incidents(
        n=n,
        app=app,
        ctx=context,
        read_incidents_fn=read_incidents,
        in_memory_incidents=_in_memory_incidents,
        make_plain_english_fn=make_plain_english,
    )
