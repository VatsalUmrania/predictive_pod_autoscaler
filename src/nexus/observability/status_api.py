"""
NEXUS Status API
=================
FastAPI application exposing:
    GET  /health                   — liveness probe
    GET  /status                   — full system snapshot
    GET  /metrics                  — Prometheus text format
    GET  /rca/last?n=10            — last N RCA decisions from Orchestrator
    GET  /runbooks/stats           — per-runbook statistics from OutcomeStore
    GET  /prescaler                — Prescaler stats + mode
    POST /prescaler/mode/{mode}    — change Prescaler autonomy mode
    GET  /learning                 — FeedbackLoop status + KPIs
    POST /learning/run             — trigger immediate feedback cycle
    GET  /audit/tail?n=20          — last N audit records
    GET  /knowledge                — KnowledgeBase adjustment table
    POST /approve/{action_id}      — approve a pending human action
    GET  /advisor                  — latest RunbookAdvisor recommendations

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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware

from nexus.observability.metrics import get_metrics

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

app = FastAPI(
    title       = "NEXUS Self-Healing Infrastructure",
    description = "Status, control, and observability API for the NEXUS system",
    version     = "2.0.0",
    docs_url    = "/docs",
    redoc_url   = "/redoc",
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
# Health + info
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["system"])
def health() -> Dict[str, Any]:
    """Liveness probe — always returns 200 if the server is up."""
    return {
        "status":           "ok",
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "uptime_seconds":   round(time.monotonic() - _start_time, 1),
    }


@app.get("/status", tags=["system"])
async def status() -> Dict[str, Any]:
    """Full system snapshot — orchestrator, governance, prescaler, learning."""
    out: Dict[str, Any] = {
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
def last_rca(n: int = 10) -> List[Dict[str, Any]]:
    """Return the N most recent RCA decisions from the Orchestrator."""
    orc = _require(context.orchestrator, "NexusOrchestrator")
    return orc.last_rca_results(n)


# ──────────────────────────────────────────────────────────────────────────────
# Runbooks
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/runbooks/stats", tags=["governance"])
async def runbook_stats(days: int = 30) -> Dict[str, Any]:
    """Per-runbook statistics from the AuditTrail (success_rate, false_heal_rate)."""
    store = _require(context.outcome_store, "OutcomeStore")
    all_stats = await store.get_all_runbook_stats(days=days)
    return {k: v.to_dict() for k, v in all_stats.items()}


@app.get("/runbooks/list", tags=["governance"])
def runbook_list() -> List[str]:
    """List all loaded runbook IDs from RunbookLibrary."""
    lib = _require(context.runbook_library, "RunbookLibrary")
    return list(lib._runbooks.keys())


# ──────────────────────────────────────────────────────────────────────────────
# Prescaler
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/prescaler", tags=["predictive"])
def prescaler_status() -> Dict[str, Any]:
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
def prescaler_set_mode(mode: str) -> Dict[str, str]:
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
def learning_status() -> Dict[str, Any]:
    """FeedbackLoop status and latest system KPIs."""
    fl = _require(context.feedback_loop, "FeedbackLoop")
    return fl.status


@app.post("/learning/run", tags=["learning"])
async def learning_run() -> Dict[str, Any]:
    """Trigger an immediate learning feedback cycle (useful for testing)."""
    fl = _require(context.feedback_loop, "FeedbackLoop")
    result = await fl.run_now()
    return {"status": "ok", **result}


@app.get("/knowledge", tags=["learning"])
async def knowledge_records() -> List[Dict[str, Any]]:
    """All KnowledgeBase confidence adjustment records."""
    kb = _require(context.knowledge_base, "KnowledgeBase")
    records = await kb.get_all_records()
    return [r.to_dict() for r in records]


@app.get("/advisor", tags=["learning"])
async def advisor_recommendations(days: int = 30) -> List[Dict[str, Any]]:
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
async def audit_tail(n: int = 20) -> List[Dict[str, Any]]:
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
async def audit_by_incident(incident_id: str) -> List[Dict[str, Any]]:
    """Return all audit records for a specific incident/correlation ID."""
    at = _require(context.audit_trail, "AuditTrail")
    return await at.query_by_incident(incident_id)


# ──────────────────────────────────────────────────────────────────────────────
# Human approvals
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/approve/{action_id}", tags=["governance"])
async def approve_action(action_id: str) -> Dict[str, str]:
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
        raise HTTPException(status_code=503, detail="HumanApprovalQueue not accessible")


@app.get("/approvals/pending", tags=["governance"])
async def pending_approvals() -> List[Dict[str, Any]]:
    """List all actions currently waiting in the HumanApprovalQueue."""
    orc = _require(context.orchestrator, "NexusOrchestrator")
    try:
        queue = orc.executor.ladder.human_queue
        return queue.pending_list()
    except AttributeError:
        return []
