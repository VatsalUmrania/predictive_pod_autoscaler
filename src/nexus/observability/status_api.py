"""
NEXUS Status API
=================
FastAPI application exposing:
    GET  /health                       — liveness probe
    GET  /status                       — full system snapshot
    GET  /metrics                      — Prometheus text format
    GET  /rca/last?n=10                — last N RCA decisions
    GET  /runbooks/stats               — per-runbook statistics
    GET  /prescaler                    — Prescaler stats + mode
    POST /prescaler/mode/{mode}        — change Prescaler autonomy mode
    GET  /learning                     — FeedbackLoop status + KPIs
    POST /learning/run                 — trigger immediate feedback cycle
    GET  /audit/tail?n=20              — last N audit records
    GET  /knowledge                    — KnowledgeBase adjustment table
    POST /approve/{action_id}          — approve a pending human action
    GET  /advisor                      — RunbookAdvisor recommendations

    --- Phase 8: Developer Integration Layer ---
    POST /apps/register                — register a new app, receive SELFHEAL_TOKEN
    GET  /apps                         — list registered apps
    POST /apps/{app}/rotate-token      — rotate a compromised token
    GET  /sdk/beacon.js                — browser SDK (served as JS)
    POST /sdk/event                    — generic SDK event
    POST /sdk/heartbeat                — app health snapshot
    POST /sdk/route-error              — per-route HTTP error
    POST /sdk/query                    — DB query report
    POST /sdk/frontend                 — browser beacon payload
    GET  /developer/incidents          — plain-English incident feed
    GET  /developer/predictions        — traffic forecast for apps
    GET  /developer/policy/{app}       — resolved selfheal.yaml policy
    PUT  /developer/policy/{app}       — runtime policy override
    DELETE /developer/policy/{app}/overrides — clear overrides

Architecture:
    All components are injected into a NexusContext at startup.
    Endpoints are served by the global `app` FastAPI instance.
    Mount with: uvicorn nexus.observability.status_api:app --port 8080

Usage:
    from nexus.observability.status_api import app, context
    context.orchestrator  = my_orchestrator
    context.prescaler     = my_prescaler
    context.feedback_loop = my_feedback_loop
    ...
    uvicorn.run(app, host="0.0.0.0", port=8080)
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware

from nexus.observability.metrics import get_metrics


# Phase 8 integration routers (imported lazily to avoid circular deps)
def _include_integration_routers(app: FastAPI) -> None:
    try:
        from nexus.integration.dashboard import router as dev_router
        from nexus.integration.sdk_ingest import router as sdk_router
        app.include_router(sdk_router)
        app.include_router(dev_router)
    except ImportError as exc:
        import logging
        logging.getLogger(__name__).warning(
            f"[StatusAPI] Integration routers not loaded: {exc}"
        )

# ──────────────────────────────────────────────────────────────────────────────
# Context container (holds all NEXUS runtime singletons)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class NexusContext:
    """
    Dependency injection container.
    Populate fields before starting uvicorn.
    Any field left as None causes its endpoint to return 503.
    """
    orchestrator:    Any = None   # NexusOrchestrator
    prescaler:       Any = None   # Prescaler
    feedback_loop:   Any = None   # FeedbackLoop
    outcome_store:   Any = None   # OutcomeStore
    knowledge_base:  Any = None   # KnowledgeBase
    audit_trail:     Any = None   # AuditTrail
    runbook_library: Any = None   # RunbookLibrary
    started_at:      float = field(default_factory=time.monotonic)

    def uptime_seconds(self) -> float:
        return round(time.monotonic() - self.started_at, 1)


# Global context — set fields before running the server
context = NexusContext()


# ──────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ──────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Initialize async resources (TokenStore + NATS) on startup."""
    import logging
    import os
    _log = logging.getLogger(__name__)

    # ── TokenStore ─────────────────────────────────────────────────────────────
    try:
        from nexus.integration.token_store import get_token_store
        store = get_token_store()
        await store.init()
    except Exception as exc:
        _log.warning(f"[StatusAPI] TokenStore init skipped: {exc}")

    # ── NATS (required for SDK events to reach the orchestrator) ───────────────
    global _nats_client
    try:
        from nexus.bus.nats_client import NATSClient
        _nats_url = os.environ.get("NATS_URL", "nats://localhost:4222")
        _nats_client = NATSClient(nats_url=_nats_url, reconnect_attempts=10)
        await _nats_client.connect()
        _log.info(f"[StatusAPI] ✅ NATS connected: {_nats_url}")
    except Exception as exc:
        _log.warning(f"[StatusAPI] ⚠️  NATS connection failed ({exc}) — SDK events won't reach orchestrator")
        _nats_client = None

    _include_integration_routers(app)
    yield

    # ── Cleanup ────────────────────────────────────────────────────────────────
    if _nats_client:
        try:
            await _nats_client.close()
        except Exception:
            pass


# Module-level NATS client — set by lifespan, read by sdk_ingest
_nats_client = None


app = FastAPI(
    title       = "NEXUS Self-Healing Infrastructure",
    description = "Status, control, and developer integration API for NEXUS",
    version     = "2.0.0",
    docs_url    = "/docs",
    redoc_url   = "/redoc",
    lifespan    = _lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_start_time = time.monotonic()


def _require(component: Any, name: str) -> Any:
    if component is None:
        raise HTTPException(status_code=503, detail=f"{name} not configured")
    return component


# ──────────────────────────────────────────────────────────────────────────────
# Developer Dashboard (served at /)
# ──────────────────────────────────────────────────────────────────────────────

_DASHBOARD_HTML = Path(__file__).parent / "developer-dashboard.html"

@app.get("/", include_in_schema=False)
def serve_dashboard() -> Response:
    """Serve the NEXUS Developer Dashboard UI at the root URL."""
    if not _DASHBOARD_HTML.exists():
        raise HTTPException(
            status_code=404,
            detail="Dashboard HTML not found. Ensure developer-dashboard.html is bundled with the package.",
        )
    return Response(
        content    = _DASHBOARD_HTML.read_text(encoding="utf-8"),
        media_type = "text/html",
        headers    = {"Cache-Control": "no-cache"},
    )


# ──────────────────────────────────────────────────────────────────────────────
# Health + info
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["system"])
def health() -> dict[str, Any]:
    """Liveness probe — always returns 200 if the server is up."""
    return {
        "status":           "ok",
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "uptime_seconds":   round(time.monotonic() - _start_time, 1),
    }



@app.get("/status", tags=["system"])
async def status() -> dict[str, Any]:
    """Full system snapshot — orchestrator, governance, prescaler, learning."""
    out: dict[str, Any] = {
        "uptime_seconds": round(time.monotonic() - _start_time, 1),
        "timestamp":      datetime.now(timezone.utc).isoformat(),
    }

    if context.orchestrator:
        out["orchestrator"] = context.orchestrator.status

    if context.prescaler:
        out["prescaler"] = context.prescaler.stats

    if context.feedback_loop:
        out["learning"] = context.feedback_loop.status

    return out


# ──────────────────────────────────────────────────────────────────────────────
# Prometheus metrics
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/metrics", tags=["observability"], include_in_schema=False)
def prometheus_metrics() -> Response:
    """Expose Prometheus metrics in text format."""
    m = get_metrics()
    return Response(
        content      = m.generate_latest(),
        media_type   = m.content_type,
    )


# ──────────────────────────────────────────────────────────────────────────────
# RCA
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/rca/last", tags=["reasoning"])
def last_rca(n: int = 10) -> list[dict[str, Any]]:
    """Return the N most recent RCA decisions from the Orchestrator."""
    orc = _require(context.orchestrator, "NexusOrchestrator")
    return orc.last_rca_results(n)


# ──────────────────────────────────────────────────────────────────────────────
# Runbooks
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/runbooks/stats", tags=["governance"])
async def runbook_stats(days: int = 30) -> dict[str, Any]:
    """Per-runbook statistics from the AuditTrail (success_rate, false_heal_rate)."""
    store = _require(context.outcome_store, "OutcomeStore")
    all_stats = await store.get_all_runbook_stats(days=days)
    return {k: v.to_dict() for k, v in all_stats.items()}


@app.get("/runbooks/list", tags=["governance"])
def runbook_list() -> list[str]:
    """List all loaded runbook IDs from RunbookLibrary."""
    lib = _require(context.runbook_library, "RunbookLibrary")
    return list(lib._runbooks.keys())


# ──────────────────────────────────────────────────────────────────────────────
# Prescaler
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/prescaler", tags=["predictive"])
def prescaler_status() -> dict[str, Any]:
    """Prescaler statistics, mode, and recent decisions."""
    p = _require(context.prescaler, "Prescaler")
    stats = p.stats
    # Last 5 decisions
    decisions = [
        {
            "id":           d.decision_id,
            "deployment":   d.deployment_name,
            "namespace":    d.namespace,
            "replicas":     f"{d.current_replicas} → {d.recommended_replicas}",
            "rps":          f"{d.current_rps:.0f} → {d.predicted_rps:.0f}",
            "confidence":   d.confidence,
            "outcome":      d.outcome or "pending",
            "decided_at":   d.decided_at,
        }
        for d in p._all_decisions[-5:][::-1]   # newest first
    ]
    return {"stats": stats, "recent_decisions": decisions}


@app.post("/prescaler/mode/{mode}", tags=["predictive"])
def prescaler_set_mode(mode: str) -> dict[str, str]:
    """Change prescaler autonomy mode: shadow | advisory | autonomous."""
    from nexus.predictive.prescaler import PrescaleMode
    p = _require(context.prescaler, "Prescaler")
    if mode not in ("shadow", "advisory", "autonomous"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid mode '{mode}'. Choose: shadow | advisory | autonomous",
        )
    result = p.promote(PrescaleMode(mode))
    return {"status": "ok", "result": result}


# ──────────────────────────────────────────────────────────────────────────────
# Learning Plane
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/learning", tags=["learning"])
def learning_status() -> dict[str, Any]:
    """FeedbackLoop status and latest system KPIs."""
    fl = _require(context.feedback_loop, "FeedbackLoop")
    return fl.status


@app.post("/learning/run", tags=["learning"])
async def learning_run() -> dict[str, Any]:
    """Trigger an immediate learning feedback cycle (useful for testing)."""
    fl = _require(context.feedback_loop, "FeedbackLoop")
    result = await fl.run_now()
    return {"status": "ok", **result}


@app.get("/knowledge", tags=["learning"])
async def knowledge_records() -> list[dict[str, Any]]:
    """All KnowledgeBase confidence adjustment records."""
    kb = _require(context.knowledge_base, "KnowledgeBase")
    records = await kb.get_all_records()
    return [r.to_dict() for r in records]


@app.get("/advisor", tags=["learning"])
async def advisor_recommendations(days: int = 30) -> list[dict[str, Any]]:
    """Run the RunbookAdvisor and return current recommendations."""
    from nexus.learning.runbook_advisor import RunbookAdvisor
    store = _require(context.outcome_store, "OutcomeStore")
    advisor = RunbookAdvisor(outcome_store=store)
    all_stats = await store.get_all_runbook_stats(days=days)
    kpis      = await store.get_system_kpis(days=days)
    recs      = advisor.analyze(all_stats, kpis)
    chronic   = await advisor.find_chronic_targets()
    recs.extend(chronic)
    return [r.to_dict() for r in recs]


# ──────────────────────────────────────────────────────────────────────────────
# Audit Trail
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/audit/tail", tags=["governance"])
async def audit_tail(n: int = 20) -> list[dict[str, Any]]:
    """Return the N most recent audit records from the AuditTrail."""
    at = _require(context.audit_trail, "AuditTrail")
    rows = await at.query_recent(limit=n)
    # Strip large JSON blobs from the tail view
    return [
        {
            k: v for k, v in row.items()
            if k not in ("pre_check_results", "action_results", "post_check_results")
        }
        for row in rows
    ]


@app.get("/audit/{incident_id}", tags=["governance"])
async def audit_by_incident(incident_id: str) -> list[dict[str, Any]]:
    """Return all audit records for a specific incident/correlation ID."""
    at = _require(context.audit_trail, "AuditTrail")
    return await at.query_by_incident(incident_id)


# ──────────────────────────────────────────────────────────────────────────────
# Human approvals
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/approve/{action_id}", tags=["governance"])
async def approve_action(action_id: str) -> dict[str, str]:
    """
    Approve a pending human-review action (from Phase 3 HumanApprovalQueue).
    Triggers the queued executor callback.
    """
    orc = _require(context.orchestrator, "NexusOrchestrator")
    try:
        ladder = orc.executor.ladder
        queue  = ladder.human_queue
        ok     = await queue.approve(action_id)
        if ok:
            return {"status": "approved", "action_id": action_id}
        raise HTTPException(status_code=404, detail=f"Action {action_id!r} not found in approval queue")
    except AttributeError:
        raise HTTPException(status_code=503, detail="HumanApprovalQueue not accessible") from None


@app.get("/approvals/pending", tags=["governance"])
async def pending_approvals() -> list[dict[str, Any]]:
    """List all actions currently waiting in the HumanApprovalQueue."""
    orc = _require(context.orchestrator, "NexusOrchestrator")
    try:
        queue = orc.executor.ladder.human_queue
        return queue.pending_list()
    except AttributeError:
        return []
