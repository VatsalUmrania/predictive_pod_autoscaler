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

    orchestrator: Any = None  # NexusOrchestrator
    prescaler: Any = None  # Prescaler
    feedback_loop: Any = None  # FeedbackLoop
    ppa_outcome_tracker: Any = None  # PpaOutcomeTracker
    outcome_store: Any = None  # OutcomeStore
    knowledge_base: Any = None  # KnowledgeBase
    audit_trail: Any = None  # AuditTrail
    runbook_library: Any = None  # RunbookLibrary
    started_at: float = field(default_factory=time.monotonic)

    def uptime_seconds(self) -> float:
        return round(time.monotonic() - self.started_at, 1)


# Global context — set fields before running the server
context = NexusContext()


# ──────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ──────────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Initialize async resources on startup.

    Skips self-init when NexusContext fields are already populated by NexusServer,
    to avoid duplicate NATS connections and parallel polling loops.
    """
    import logging
    import os

    _log = logging.getLogger(__name__)

    # Globals written by both paths — declare at top to satisfy Python's scoping rules
    global _ppa_outcome_tracker, _nats_client

    # ── Skip self-init: NexusServer already owns all components ─────────────────
    if context.orchestrator is not None:
        _log.info("[StatusAPI] NexusContext pre-populated — skipping self-init")
        # Wire module-level tracker so /ppa/decisions and /ppa/stats work
        _ppa_outcome_tracker = context.ppa_outcome_tracker
        _include_integration_routers(app)
        yield
        return

    # ── Self-init path (standalone status_api runs only) ─────────────────────
    try:
        from nexus.integration.token_store import get_token_store

        store = get_token_store()
        await store.init()
    except Exception as exc:
        _log.warning(f"[StatusAPI] TokenStore init skipped: {exc}")

    # ── NATS (required for SDK events and PPA outcome tracking) ─────────────────
    try:
        from nexus.bus.nats_client import NATSClient

        _nats_url = os.environ.get("NATS_URL", "nats://localhost:4222")
        _nats_client = NATSClient(nats_url=_nats_url, reconnect_attempts=10)
        await _nats_client.connect()
        _log.info(f"[StatusAPI] ✅ NATS connected: {_nats_url}")
    except Exception as exc:
        _log.warning(
            f"[StatusAPI] ⚠️  NATS connection failed ({exc}) — SDK events won't reach orchestrator"
        )
        _nats_client = None

    # ── PPA Outcome Tracker ────────────────────────────────────────────────────
    _ppa_outcome_tracker = None
    if _nats_client:
        try:
            from nexus.learning.ppa_outcome_tracker import PpaOutcomeTracker

            _ppa_outcome_tracker = PpaOutcomeTracker(nats_client=_nats_client)
            await _ppa_outcome_tracker.start()
            # Subscribe to PPA prediction events (raw JSON — not IncidentEvent)
            await _nats_client.subscribe_raw(
                "ppa.predictions.>",
                handler=_on_ppa_prediction,
                stream_name="PPA_PREDICTIONS",
            )
            _log.info("[StatusAPI] ✅ PPA OutcomeTracker started")

            # ── Prescaler (ppa.predictions → pre-scale decision engine) ─────────
            try:
                from nexus.predictive.prescaler import Prescaler

                context.prescaler = Prescaler(nats_client=_nats_client)
                await context.prescaler.subscribe_to_ppa_predictions()
                _log.info("[StatusAPI] ✅ Prescaler subscribed to ppa.predictions.*")
            except Exception as exc:
                _log.warning(
                    f"[StatusAPI] ⚠️  Prescaler init failed ({exc}) — pre-scale decisions disabled"
                )
        except Exception as exc:
            _log.warning(f"[StatusAPI] ⚠️  PPA OutcomeTracker init failed ({exc})")

    _include_integration_routers(app)
    yield

    # ── Cleanup ────────────────────────────────────────────────────────────────
    if _ppa_outcome_tracker:
        try:
            await _ppa_outcome_tracker.stop()
        except Exception:
            pass
    if _nats_client:
        try:
            await _nats_client.close()
        except Exception:
            pass


# Module-level tracker — set by lifespan
_ppa_outcome_tracker = None


async def _on_ppa_prediction(data: dict, subject: str) -> None:
    """Handle incoming ppa.predictions.* events — delegate to OutcomeTracker.

    Called by subscribe_raw() with already-decoded JSON dict + subject string.
    Subject format: ppa.predictions.<deployment>
    """
    if _ppa_outcome_tracker is None:
        return
    try:
        subject_parts = subject.split(".")
        deployment = subject_parts[-1] if len(subject_parts) > 2 else "unknown"
        await _ppa_outcome_tracker.on_prediction(
            deployment=deployment,
            namespace=data.get("namespace", "default"),
            predicted_rps=float(data.get("predicted_rps", 0)),
            current_rps=float(data.get("current_rps", 0)),
            confidence=float(data.get("confidence", 0)),
            horizon_minutes=int(data.get("horizon_minutes", 10)),
            model_version=data.get("model_version", "unknown"),
            raw_features=data.get("raw_features", {}),
            event_timestamp=data.get("timestamp", ""),
        )
    except Exception as exc:
        import logging

        logging.getLogger(__name__).warning(f"[_on_ppa_prediction] {exc}")


# Module-level NATS client — set by lifespan, read by sdk_ingest
_nats_client = None


app = FastAPI(
    title="NEXUS Self-Healing Infrastructure",
    description="Status, control, and developer integration API for NEXUS",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=_lifespan,
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
        content=_DASHBOARD_HTML.read_text(encoding="utf-8"),
        media_type="text/html",
        headers={"Cache-Control": "no-cache"},
    )


# ──────────────────────────────────────────────────────────────────────────────
# Health + info
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/health", tags=["system"])
def health() -> dict[str, Any]:
    """Liveness probe — always returns 200 if the server is up."""
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "uptime_seconds": round(time.monotonic() - _start_time, 1),
    }


@app.get("/status", tags=["system"])
async def status() -> dict[str, Any]:
    """Full system snapshot — orchestrator, governance, prescaler, learning."""
    out: dict[str, Any] = {
        "uptime_seconds": round(time.monotonic() - _start_time, 1),
        "timestamp": datetime.now(timezone.utc).isoformat(),
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
        content=m.generate_latest(),
        media_type=m.content_type,
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
            "id": d.decision_id,
            "deployment": d.deployment_name,
            "namespace": d.namespace,
            "replicas": f"{d.current_replicas} → {d.recommended_replicas}",
            "rps": f"{d.current_rps:.0f} → {d.predicted_rps:.0f}",
            "confidence": d.confidence,
            "outcome": d.outcome or "pending",
            "decided_at": d.decided_at,
        }
        for d in p._all_decisions[-5:][::-1]  # newest first
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
    kpis = await store.get_system_kpis(days=days)
    recs = advisor.analyze(all_stats, kpis)
    chronic = await advisor.find_chronic_targets()
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
            k: v
            for k, v in row.items()
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
        queue = ladder.approval_queue
        ok = queue.approve(action_id)
        if ok:
            if context.audit_trail:
                await context.audit_trail.record_approval(action_id, "api_user")
            return {"status": "approved", "action_id": action_id}
        raise HTTPException(
            status_code=404, detail=f"Action {action_id!r} not found in approval queue"
        )
    except AttributeError:
        raise HTTPException(
            status_code=503, detail="HumanApprovalQueue not accessible"
        ) from None


@app.post("/reject/{action_id}", tags=["governance"])
async def reject_action(action_id: str) -> dict[str, str]:
    """
    Reject a pending human-review action.
    """
    orc = _require(context.orchestrator, "NexusOrchestrator")
    try:
        ladder = orc.executor.ladder
        queue = ladder.approval_queue
        ok = queue.reject(action_id)
        if ok:
            if context.audit_trail:
                await context.audit_trail.record_rejection(action_id, "api_user")
            return {"status": "rejected", "action_id": action_id}
        raise HTTPException(
            status_code=404, detail=f"Action {action_id!r} not found in approval queue"
        )
    except AttributeError:
        raise HTTPException(
            status_code=503, detail="HumanApprovalQueue not accessible"
        ) from None


@app.get("/approvals/pending", tags=["governance"])
async def pending_approvals() -> list[dict[str, Any]]:
    """List all actions currently waiting in the HumanApprovalQueue."""
    orc = _require(context.orchestrator, "NexusOrchestrator")
    try:
        queue = orc.executor.ladder.approval_queue
        return queue.pending_list()
    except AttributeError:
        return []


# ──────────────────────────────────────────────────────────────────────────────
# PPA Integration Endpoints
# Backed by the PpaOutcomeTracker started in the lifespan.
# These are available without a full Orchestrator / Prescaler — the
# OutcomeTracker subscribes to ppa.predictions.* via NATS directly.
# ──────────────────────────────────────────────────────────────────────────────


@app.get("/ppa/decisions", tags=["predictive"])
def ppa_decisions(n: int = 20) -> list[dict[str, Any]]:
    """
    Recent PPA prediction events received from the PPA operator via NATS.

    Each entry represents one ppa.predictions.* message recorded by the
    PpaOutcomeTracker. Outcomes (verdict + SMAPE) are back-filled after
    the prediction horizon elapses.

    Returns [] when no events have been received yet (normal on first startup
    before the PPA operator publishes its first cycle).
    """
    tracker = _ppa_outcome_tracker
    if tracker is None:
        return []

    # Combine pending (unresolved) + resolved outcomes, newest first
    pending = [
        {
            "decision_id": p.decision_id,
            "deployment": p.deployment,
            "namespace": p.namespace,
            "predicted_rps": round(p.predicted_rps, 1),
            "current_rps": round(p.current_rps, 1),
            "confidence": round(p.confidence, 3),
            "horizon_minutes": p.horizon_minutes,
            "model_version": p.model_version,
            "status": "pending",
            "verdict": None,
            "smape": None,
            "created_at": p.created_at.isoformat(),
            "resolves_at": p.expected_resolution_time.isoformat(),
        }
        for p in tracker._pending.values()
    ]

    resolved = [
        {
            "decision_id": o.get("decision_id"),
            "deployment": o.get("deployment"),
            "namespace": o.get("namespace"),
            "predicted_rps": o.get("predicted_rps"),
            "current_rps": None,  # not stored in outcome event
            "confidence": o.get("confidence"),
            "horizon_minutes": None,
            "model_version": o.get("model_version"),
            "status": "resolved",
            "verdict": o.get("verdict"),
            "smape": o.get("smape"),
            "created_at": o.get("resolution_at"),
            "resolves_at": None,
        }
        for o in tracker._recent_outcomes
    ]

    all_decisions = pending + resolved
    # Sort newest first (pending have created_at; resolved have resolution_at)
    all_decisions.sort(
        key=lambda d: (d.get("created_at") or d.get("resolves_at") or ""),
        reverse=True,
    )
    return all_decisions[:n]


@app.get("/ppa/stats", tags=["predictive"])
def ppa_stats() -> dict[str, Any]:
    """
    Aggregated PPA prediction statistics from the PpaOutcomeTracker.

    Returns counts of pending / resolved predictions, mean SMAPE,
    and spike hit-rate so you can gauge model quality without Grafana.
    """
    tracker = _ppa_outcome_tracker
    if tracker is None:
        return {
            "tracker_ready": False,
            "nats_connected": False,
            "message": (
                "PpaOutcomeTracker not initialised — "
                "check NATS_URL in the nexus-api container."
            ),
        }

    resolved = tracker._recent_outcomes
    pending = list(tracker._pending.values())

    total_resolved = len(resolved)
    spike_hits = sum(1 for o in resolved if o.get("verdict") == "spike_hit")
    spike_misses = sum(1 for o in resolved if o.get("verdict") == "spike_missed")
    smape_vals = [o["smape"] for o in resolved if o.get("smape") is not None]
    mean_smape = round(sum(smape_vals) / len(smape_vals), 3) if smape_vals else None
    hit_rate = round(spike_hits / total_resolved, 3) if total_resolved else None

    return {
        "tracker_ready": True,
        "nats_connected": _nats_client is not None,
        "pending_count": len(pending),
        "resolved_count": total_resolved,
        "spike_hits": spike_hits,
        "spike_misses": spike_misses,
        "correct_no_spike": total_resolved - spike_hits - spike_misses,
        "mean_smape": mean_smape,
        "spike_hit_rate": hit_rate,
        "ready_for_advisory": (hit_rate or 0) >= 0.7 and total_resolved >= 10,
    }


@app.get("/ppa/pending", tags=["predictive"])
def ppa_pending() -> list[dict[str, Any]]:
    """Predictions currently awaiting their horizon window to elapse."""
    tracker = _ppa_outcome_tracker
    if tracker is None:
        return []
    return [
        {
            "decision_id": p.decision_id,
            "deployment": p.deployment,
            "predicted_rps": round(p.predicted_rps, 1),
            "current_rps": round(p.current_rps, 1),
            "confidence": round(p.confidence, 3),
            "horizon_minutes": p.horizon_minutes,
            "resolves_at": p.expected_resolution_time.isoformat(),
            "is_expired": p.is_expired,
        }
        for p in tracker._pending.values()
    ]
