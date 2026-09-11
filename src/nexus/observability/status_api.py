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

import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from nexus.integration.notifier import SLACK_SIGNING_SECRET
from nexus.observability.metrics import get_metrics

# Phase 8 integration routers (imported lazily to avoid circular deps)
_routers_included = False

def _include_integration_routers(app: FastAPI) -> None:
    global _routers_included
    if _routers_included:
        return
    try:
        from nexus.integration.dashboard import router as dev_router
        from nexus.integration.sdk_ingest import router as sdk_router

        if not any(getattr(r, "path", None) == "/developer/incidents" for r in app.routes):
            app.include_router(sdk_router)
            app.include_router(dev_router)
        _routers_included = True
    except ImportError as exc:
        import logging

        logging.getLogger(__name__).warning(
            f"[StatusAPI] Integration routers not loaded: {exc}"
        )

# Context container (holds all NEXUS runtime singletons)
@dataclass
class NexusContext:
    """
    Dependency injection container.
    Populate fields before starting uvicorn.
    Any field left as None causes its endpoint to return 503.
    """

    workflow: Any = None  # IncidentWorkflow (LangGraph)
    orchestrator: Any = None  # IncidentWorkflow or NexusOrchestrator
    prescaler: Any = None  # Prescaler
    feedback_loop: Any = None  # FeedbackLoop
    ppa_outcome_tracker: Any = None  # PpaOutcomeTracker
    outcome_store: Any = None  # OutcomeStore
    knowledge_base: Any = None  # KnowledgeBase
    audit_trail: Any = None  # AuditTrail
    runbook_library: Any = None  # RunbookLibrary (deprecated)
    db_client: Any = None  # PostgresClient
    started_at: float = field(default_factory=time.monotonic)

    def uptime_seconds(self) -> float:
        return round(time.monotonic() - self.started_at, 1)

# Logger
_log = logging.getLogger(__name__)

# Global context — set fields before running the server
context = NexusContext()

# FastAPI app
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

    # Skip self-init: NexusServer already owns all components
    if context.orchestrator is not None:
        _log.info("[StatusAPI] NexusContext pre-populated — skipping self-init")
        # Wire module-level tracker so /ppa/decisions and /ppa/stats work
        _ppa_outcome_tracker = context.ppa_outcome_tracker
        _include_integration_routers(app)
        yield
        return

    # Self-init path (standalone status_api runs only)
    try:
        from nexus.db.postgres import get_database_client

        context.db_client = await get_database_client()
        _log.info("[StatusAPI] PostgreSQL client connected")
    except Exception as exc:
        _log.warning(f"[StatusAPI] Database init skipped/failed ({exc})")

    try:
        from nexus.integration.token_store import get_token_store

        store = get_token_store()
        await store.init()
    except Exception as exc:
        _log.warning(f"[StatusAPI] TokenStore init skipped: {exc}")

    # NATS (required for SDK events and PPA outcome tracking)
    try:
        from nexus.bus.nats_client import NATSClient

        _nats_url = os.environ.get("NATS_URL", "nats://localhost:4222")
        _nats_client = NATSClient(nats_url=_nats_url, reconnect_attempts=10)
        await _nats_client.connect()
        _log.info(f"[StatusAPI] NATS connected: {_nats_url}")
    except Exception as exc:
        _log.warning(
            f"[StatusAPI] NATS connection failed ({exc}) — SDK events won't reach orchestrator"
        )
        _nats_client = None

    # PPA Outcome Tracker
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
            _log.info("[StatusAPI] PPA OutcomeTracker started")

            # Prescaler (ppa.predictions → pre-scale decision engine)
            try:
                from nexus.predictive.prescaler import Prescaler

                context.prescaler = Prescaler(nats_client=_nats_client)
                await context.prescaler.subscribe_to_ppa_predictions()
                _log.info("[StatusAPI] Prescaler subscribed to ppa.predictions.*")
            except Exception as exc:
                _log.warning(
                    f"[StatusAPI] Prescaler init failed ({exc}) — pre-scale decisions disabled"
                )
        except Exception as exc:
            _log.warning(f"[StatusAPI] PPA OutcomeTracker init failed ({exc})")

    _include_integration_routers(app)
    yield

    # Cleanup
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
_nats_client: Any = None

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

_include_integration_routers(app)

_start_time = time.monotonic()


def _require(component: Any, name: str) -> Any:
    if component is None:
        raise HTTPException(status_code=503, detail=f"{name} not configured")
    return component

# Developer Dashboard (served at /)

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

# Health + info
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

# Prometheus metrics
@app.get("/metrics", tags=["observability"], include_in_schema=False)
def prometheus_metrics() -> Response:
    """Expose Prometheus metrics in text format."""
    m = get_metrics()
    return Response(
        content=m.generate_latest(),
        media_type=m.content_type,
    )

# RCA
@app.get("/rca/last", tags=["reasoning"])
def last_rca(n: int = 10) -> list[dict[str, Any]]:
    """Return the N most recent RCA decisions from the LangGraph workflow."""
    if context.workflow and hasattr(context.workflow, "last_rca_results"):
        return cast(list[dict[str, Any]], context.workflow.last_rca_results(n))
    if context.orchestrator and hasattr(context.orchestrator, "last_rca_results"):
        return cast(list[dict[str, Any]], context.orchestrator.last_rca_results(n))
    return []

# Runbooks
@app.get("/runbooks/stats", tags=["governance"])
async def runbook_stats(days: int = 30) -> dict[str, Any]:
    """Per-runbook statistics from the AuditTrail (deprecated in LangGraph mode)."""
    if not context.outcome_store:
        return {}
    all_stats = await context.outcome_store.get_all_runbook_stats(days=days)
    return {k: v.to_dict() for k, v in all_stats.items()}

@app.get("/runbooks/list", tags=["governance"])
def runbook_list() -> list[str]:
    """List loaded runbook IDs (deprecated in LangGraph mode)."""
    if context.runbook_library:
        return list(context.runbook_library._runbooks.keys())
    return []

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

# Learning Plane
@app.get("/learning", tags=["learning"])
def learning_status() -> dict[str, Any]:
    """FeedbackLoop status and latest system KPIs."""
    fl = _require(context.feedback_loop, "FeedbackLoop")
    return cast(dict[str, Any], fl.status)


@app.get("/advisor", tags=["learning"])
async def advisor_recommendations(days: int = 30) -> list[dict[str, Any]]:
    """Return recommendations (deprecated in LangGraph mode)."""
    return []

@app.get("/audit/tail", tags=["governance"])
async def audit_tail(n: int = 20) -> list[dict[str, Any]]:
    """Return the N most recent audit trail records."""
    at = _require(context.audit_trail, "AuditTrail")
    return cast(list[dict[str, Any]], await at.tail(n))


@app.get("/audit/incident/{incident_id}", tags=["governance"])
async def audit_by_incident(incident_id: str) -> list[dict[str, Any]]:
    """Return all audit records for a specific incident/correlation ID."""
    at = _require(context.audit_trail, "AuditTrail")
    return cast(list[dict[str, Any]], await at.query_by_incident(incident_id))

# Human approvals
@app.post("/approve/{action_id}", tags=["governance"])
async def approve_action(action_id: str) -> dict[str, Any]:
    """Approve a pending human-review action in LangGraph workflow."""
    if context.workflow and hasattr(context.workflow, "resume_incident"):
        is_wf_target = (
            hasattr(context.workflow, "has_pending")
            and context.workflow.has_pending(action_id)
        ) or not context.orchestrator

        if is_wf_target:
            try:
                outcome = await context.workflow.resume_incident(action_id, approval_decision="approved")
                if context.audit_trail:
                    await context.audit_trail.record_approval(action_id, "api_user")
                return {"status": "approved", "action_id": action_id, "outcome": outcome}
            except Exception as exc:
                _log.warning(f"Workflow approve failed for {action_id}: {exc}")
                return {"status": "approved", "action_id": action_id, "error": str(exc)}

    # Fallback to legacy orchestrator if configured
    orc = context.orchestrator
    if orc and hasattr(orc, "executor"):
        try:
            ladder = orc.executor.ladder
            queue = ladder.approval_queue
            if queue.is_approved(action_id):
                return {"status": "already_approved", "action_id": action_id}
            pending_item = queue.get(action_id)
            if queue.approve(action_id):
                if context.audit_trail:
                    await context.audit_trail.record_approval(action_id, "api_user")
                if pending_item:
                    import asyncio
                    asyncio.create_task(ladder.dispatch_approved(pending_item))
                return {"status": "approved", "action_id": action_id}
        except Exception as orc_exc:
            _log.debug(f"[StatusAPI] Legacy orchestrator approve failed: {orc_exc}")

    return {"status": "approved", "action_id": action_id}


@app.post("/reject/{action_id}", tags=["governance"])
async def reject_action(action_id: str) -> dict[str, str]:
    """
    Reject a pending human-review action.
    """
    if context.workflow and hasattr(context.workflow, "resume_incident"):
        is_wf_target = (
            hasattr(context.workflow, "has_pending")
            and context.workflow.has_pending(action_id)
        ) or not context.orchestrator

        if is_wf_target:
            try:
                await context.workflow.resume_incident(action_id, approval_decision="rejected")
                if context.audit_trail:
                    await context.audit_trail.record_rejection(action_id, "api_user")
                return {"status": "rejected", "action_id": action_id}
            except Exception as wf_exc:
                _log.info(f"[StatusAPI] Workflow reject attempted for {action_id}: {wf_exc}")

    orc = context.orchestrator
    if not orc:
        return {"status": "rejected", "action_id": action_id}
    try:
        ladder = orc.executor.ladder
        queue = ladder.approval_queue
        # Idempotent: a repeat reject re-INSERTs the audit row (PK reject_{id}).
        if queue.is_rejected(action_id):
            return {"status": "already_rejected", "action_id": action_id}
        pending_item = queue.get(action_id)
        if queue.reject(action_id):
            if context.audit_trail:
                await context.audit_trail.record_rejection(action_id, "api_user")
            if pending_item and hasattr(orc, "notify_incident_rejected"):
                import asyncio
                res = orc.notify_incident_rejected(
                    pending_item.target,
                    pending_item.incident_id,
                    reason="Rejected via HTTP API",
                )
                if asyncio.iscoroutine(res):
                    await res
            return {"status": "rejected", "action_id": action_id}
        raise HTTPException(
            status_code=404, detail=f"Action {action_id!r} not found in approval queue"
        )
    except AttributeError:
        raise HTTPException(
            status_code=503, detail="HumanApprovalQueue not accessible"
        ) from None

# ── Slack interactivity callback ─────────────────────────────────────────────
# Receives Approve/Reject button clicks from the interactive messages sent by
# Notifier.notify_approval_required / chatops.send_approval_request. Verifies
# Slack's request signature, then routes the decision through the SAME
# approval/reject logic the CLI and /approve /reject HTTP endpoints use.
def _slack_replace(text: str) -> dict[str, Any]:
    """Replace the original interactive message so the buttons disappear.

    Leaving Approve/Reject active after a decision invites a second operator to
    act on a resolved action. The queue's idempotency checks already prevent a
    double-dispatch, but the UX should reflect the decision too.
    """
    return {"replace_original": True, "text": text}

async def _slack_replace_via_response_url(
    response_url: str | None, text: str
) -> None:
    """POST a ``replace_original`` payload to Slack's ``response_url``.

    Slack only honors the *synchronous* response-body ``replace_original`` when
    it lands within ~3s. The approve path does real governance + k8s work
    (ActionLadder → execute → post-checks) that routinely exceeds that window,
    so its sync replace was dropped and the Approve/Reject buttons stayed live
    even though the backend approved. The ``response_url`` Slack embeds in every
    interactive payload accepts the replacement up to 30 min later and is the
    documented mechanism for slow handlers, so we ack the click fast (the placeholder
    is returned inline) and push the real outcome here. Best-effort;
    a POST failure is logged, never raised — the approval still landed in the
    queue and audit trail regardless.
    """
    if not response_url:
        return
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                response_url, json={"replace_original": True, "text": text}
            )
    except Exception as exc:
        _log.warning(f"Failed to push delayed outcome to Slack response_url: {exc}")


async def _slack_dispatch_approved(
    orc: Any,
    pending_item: Any,
    approval_id: str,
    user: str,
    response_url: str | None,
) -> None:
    """Background: run the approved action through the governance plane, then
    update the Slack card with the outcome. Runs AFTER the fast ack is sent,
    so Slack's ~3s interactive-response limit is never breached by dispatch."""
    try:
        outcome = await orc.executor.execute_approved(pending_item)
        text = (
            f"Approved `{approval_id}` by @{user} → {outcome['status']} "
            f"(audit {outcome.get('action_id')})"
        )
    except Exception as exc:
        _log.error(f"Slack approve dispatch failed: {exc}")
        text = f"Approved `{approval_id}` by @{user} but dispatch errored: {exc}"
    await _slack_replace_via_response_url(response_url, text)

@app.post("/slack/interactive", tags=["governance"])
async def slack_interactive(
    request: Request, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """Slack interactive callback handler (Approve / Reject button clicks).

    Slack POSTs ``application/x-www-form-urlencoded`` with a ``payload`` form
    field whose JSON value looks like:

        {
            "type": "block_actions",
            "user": {"id": "U123", "username": "vatsal", "name": "Vatsal"},
            "actions": [{"action_id": "nexus_approve", "value": "<approval_id>"}],
            "response_url": "https://hooks.slack.com/actions/...",
            ...
        }

    The raw body is HMAC-verified against the Slack app's signing secret before
    any parsing — this is the trust boundary, so a bad signature is a hard 401.
    We then parse the form body with stdlib (no python-multipart needed) and
    route to the same approve/reject path used everywhere else, recording the
    Slack username as the auditor. The approve path acks immediately with a
    placeholder (Slack caps interactive responses at ~3s — awaiting the
    governance dispatch inline would breach that and the card stays stuck on
    live buttons), then backgrounds execute_approved() and posts the real
    outcome to Slack's response_url (accepts the replacement up to 30 min
    later, no 3s limit).
    """
    import json
    from urllib.parse import parse_qs

    from nexus.integration.notifier import verify_slack_request

    raw = await request.body()
    _log.info(f"Slack interactivity: received raw body length={len(raw)}")
    _log.debug(f"Slack interactivity: raw body={raw[:500]!r}")
    _log.debug(f"Slack interactivity: headers={dict(request.headers)}")

    ts = request.headers.get("X-Slack-Request-Timestamp", "missing")
    sig = request.headers.get("X-Slack-Signature", "missing")
    _log.info(f"Slack interactivity: ts={ts}, sig_prefix={sig[:20] if sig != 'missing' else 'missing'}...")

    if not verify_slack_request(raw, request.headers):
        _log.warning("Slack interactivity: INVALID SIGNATURE - rejecting (401)")
        _log.warning(f"  Expected sig check: SLACK_SIGNING_SECRET={'set' if SLACK_SIGNING_SECRET else 'MISSING!'}")
        _log.warning(f"  ts={ts}, body_len={len(raw)}")
        raise HTTPException(status_code=401, detail="invalid_signature")

    _log.info("Slack interactivity: signature VERIFIED OK")

    parsed = parse_qs(raw.decode("utf-8")).get("payload")
    payload = parsed[0] if parsed else None
    _log.info(f"Slack interactivity: payload field present={payload is not None}")
    if not payload:
        _log.warning("Slack interactivity: missing payload field")
        raise HTTPException(status_code=400, detail="missing payload")

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        _log.warning("Slack interactivity: invalid JSON in payload")
        raise HTTPException(status_code=400, detail="invalid payload") from None

    actions = data.get("actions", [])
    if not actions:
        _log.warning("Slack interactivity: no actions in payload")
        raise HTTPException(status_code=400, detail="no actions")

    action = actions[0]
    action_id = action.get("action_id")  # "nexus_approve" / "nexus_reject"
    approval_id = action.get("value")  # the approval UUID
    user = (
        data.get("user", {}).get("username")
        or data.get("user", {}).get("id", "unknown")
    )
    _log.info(f"Slack interactivity: action_id={action_id}, approval_id={approval_id}, user={user}")
    response_url = data.get("response_url")
    _log.info(f"Slack interactivity: response_url={'present' if response_url else 'absent'}")

    if not approval_id or action_id not in ("nexus_approve", "nexus_reject"):
        _log.warning(
            f"Slack interactivity: malformed action={action_id} value={approval_id}"
        )
        raise HTTPException(status_code=400, detail="malformed action")

    # Check if this pending approval belongs to the LangGraph workflow
    if context.workflow and hasattr(context.workflow, "resume_incident") and hasattr(context.workflow, "has_pending") and context.workflow.has_pending(approval_id):
        if action_id == "nexus_approve":
            background_tasks.add_task(context.workflow.resume_incident, approval_id, "approved")
            if context.audit_trail:
                await context.audit_trail.record_approval(approval_id, f"slack:{user}")
            return _slack_replace(f"Approved `{approval_id}` by @{user} — executing remediation…")
        elif action_id == "nexus_reject":
            background_tasks.add_task(context.workflow.resume_incident, approval_id, "rejected")
            if context.audit_trail:
                await context.audit_trail.record_rejection(approval_id, f"slack:{user}")
            return _slack_replace(f"Rejected `{approval_id}` by @{user}.")

    orc = _require(context.orchestrator, "NexusOrchestrator")
    try:
        queue = orc.executor.ladder.approval_queue
    except AttributeError:
        _log.warning("Slack interactivity: approval queue not accessible")
        raise HTTPException(status_code=503, detail="queue unavailable") from None

    # Approve: re-dispatch through the governance plane (same as /approve/{id})
    if action_id == "nexus_approve":
        if queue.is_approved(approval_id):
            return _slack_replace(f"Action `{approval_id}` was already approved.")
        # Snapshot BEFORE approve(): once approved, pending_list() excludes the
        # id (it filters out approved/rejected), so a post-approve lookup would
        # lose the staged item and never re-dispatch. Mirrors /approve/{id}.
        pending_item = next(
            (p for p in queue.pending_list() if p.approval_id == approval_id), None
        )
        if not queue.approve(approval_id):
            return _slack_replace(f"Action `{approval_id}` not found.")
        if context.audit_trail:
            await context.audit_trail.record_approval(approval_id, f"slack:{user}")
        if pending_item is None:
            return _slack_replace(
                f"Approved `{approval_id}` by @{user} "
                f"(staged snapshot lost — queued only)."
            )
        # Acknowledge the click fast: the placeholder is the synchronous
        # response, so it replaces the card within Slack's ~3s interactive
        # window (buttons vanish — the click registered). The real governance
        # dispatch (ActionLadder + k8s execute + post-checks) routinely takes
        # longer than 3s; awaiting it inline made Slack drop the synchronous
        # replace and the card stayed stuck on live buttons. So we run it in a
        # background task and POST the real outcome to Slack's response_url,
        # which accepts the replacement up to 30 min later.
        background_tasks.add_task(
            _slack_dispatch_approved,
            orc, pending_item, approval_id, user, response_url,
        )
        return _slack_replace(
            f"Approved `{approval_id}` by @{user} — executing…"
        )

    # Reject: record and stop the workflow
    if queue.is_rejected(approval_id):
        return _slack_replace(f"Action `{approval_id}` was already rejected.")
    pending_item = queue.get(approval_id)
    if not queue.reject(approval_id):
        return _slack_replace(f"Action `{approval_id}` not found.")
    if context.audit_trail:
        await context.audit_trail.record_rejection(approval_id, f"slack:{user}")
    if pending_item and orc and hasattr(orc, "notify_incident_rejected"):
        import asyncio
        res = orc.notify_incident_rejected(
            pending_item.target,
            pending_item.incident_id,
            reason=f"Rejected via Slack by @{user}",
        )
        if asyncio.iscoroutine(res):
            await res
    return _slack_replace(f"Rejected `{approval_id}` by @{user}.")


@app.get("/approvals/pending", tags=["governance"])
async def pending_approvals() -> list[dict[str, Any]]:
    """List all actions currently waiting in the HumanApprovalQueue or LangGraph workflow."""
    results: list[dict[str, Any]] = []

    # 1. First check LangGraph workflow pending approvals
    if context.workflow and hasattr(context.workflow, "pending_approvals"):
        try:
            wf_pending = context.workflow.pending_approvals()
            if wf_pending:
                results.extend(wf_pending)
        except Exception as exc:
            _log.warning(f"[StatusAPI] Failed querying workflow pending approvals: {exc}")

    # 2. Check legacy orchestrator queue if configured
    if context.orchestrator:
        try:
            queue = context.orchestrator.executor.ladder.approval_queue
            # pending_list() returns PendingApproval dataclasses; serialize so the
            # declared list[dict] response model validates under pydantic v2.
            results.extend([p.to_dict() for p in queue.pending_list()])
        except Exception:
            pass

    return results


# AlertManager Webhook Integration
@app.post("/webhook/alertmanager", tags=["integration"])
async def alertmanager_webhook(payload: dict[str, Any]) -> dict[str, str]:
    """
    Receive alerts from Prometheus Alertmanager and trigger the LLM Orchestrator.
    """
    try:
        import asyncio

        from nexus.reasoning.llm_orchestrator import orchestrator

        # Fire and forget the LLM handling so we don't block the webhook response
        asyncio.create_task(orchestrator.handle_alert(payload))
        return {"status": "accepted"}
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Failed to process alert webhook: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error processing webhook") from e


