"""
NEXUS SDK Ingest Router
========================
FastAPI router that receives telemetry from all SDK clients (Python, Node.js,
browser beacon) and translates them into IncidentEvents on NATS.

Mounted on the main Status API at the app root level (no prefix needed —
/sdk/* and /apps/* paths are distinct from the existing API).

Authentication:
    All /sdk/* endpoints require:
        Authorization: Bearer <SELFHEAL_TOKEN>
    Token is validated via TokenStore.validate_token().
    Returns 401 if token is missing or invalid.

Endpoints:
    POST /apps/register         — Register a new app, receive SELFHEAL_TOKEN
    GET  /apps                  — List registered apps (ops use)
    GET  /sdk/beacon.js         — Serve browser beacon.js
    POST /sdk/event             — Generic SDK event
    POST /sdk/heartbeat         — App health snapshot
    POST /sdk/route-error       — Per-route HTTP error (from backend middleware)
    POST /sdk/query             — DB query report (from wrapDB)
    POST /sdk/frontend          — Browser beacon payload (JS errors, Web Vitals)

Event translation:
    SDK payload → IncidentEvent → NATS subject: nexus.incidents.sdk.<type>
    The Orchestrator receives and correlates these alongside infra signals.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field

from nexus.integration.token_store import get_token_store

logger = logging.getLogger(__name__)

router = APIRouter()

# Path to beacon.js — relative to this file
_BEACON_JS_PATH = Path(__file__).parent.parent / "sdk" / "js" / "beacon.js"

# ── Lazy NATS singleton for SDK event publishing ──────────────────────────────
# Independent of status_api lifecycle — connects on first use.
# Publishes to nexus.incidents.sdk.<type> matching demo orchestrator subscription.
_sdk_nats_nc   = None
_sdk_nats_lock = None

async def _get_nats():
    """Return (or lazily create) a raw NATS core connection for SDK publishing."""
    global _sdk_nats_nc, _sdk_nats_lock
    if _sdk_nats_lock is None:
        _sdk_nats_lock = asyncio.Lock()
    async with _sdk_nats_lock:
        if _sdk_nats_nc is None or _sdk_nats_nc.is_closed:
            try:
                import nats as _nats_lib
                _url = os.environ.get("NATS_URL", "nats://localhost:4222")
                _sdk_nats_nc = await _nats_lib.connect(
                    _url,
                    name="nexus-sdk-ingest",
                    max_reconnect_attempts=5,
                )
                print(f"[SDKIngest] ✅ NATS connected: {_url}", flush=True)
            except Exception as _e:
                print(f"[SDKIngest] ⚠️  NATS connection failed: {_e}", flush=True)
                _sdk_nats_nc = None
        return _sdk_nats_nc

# ──────────────────────────────────────────────────────────────────────────────
# Auth dependency
# ──────────────────────────────────────────────────────────────────────────────

async def _get_app_name(
    authorization: str | None = Header(None),
) -> str:
    """Validate Bearer token and return app_name."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization: Bearer <token>")
    token    = authorization[len("Bearer "):]
    store    = get_token_store()
    app_name = await store.validate_token(token)
    if not app_name:
        raise HTTPException(status_code=401, detail="Invalid or unknown SELFHEAL_TOKEN")
    return app_name


# ──────────────────────────────────────────────────────────────────────────────
# App registration
# ──────────────────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    app_name: str = Field(..., min_length=1)
    tier:     str = Field("production")
    contact:  str | None = None          # Optional: email / Slack handle of DRI


@router.post("/apps/register", tags=["integration"])
async def register_app(req: RegisterRequest) -> dict[str, str]:
    """
    Register a new application with NEXUS and receive a SELFHEAL_TOKEN.

    This endpoint is unauthenticated (no token yet). After calling this,
    set the returned token as SELFHEAL_TOKEN in your app's environment.

    If the app is already registered, the existing token is returned
    (idempotent — safe to call repeatedly from CI/CD pipelines).
    """
    store = get_token_store()
    token = await store.register_app(req.app_name, tier=req.tier)
    logger.info(f"[SDKIngest] App registered: {req.app_name} (tier={req.tier})")
    return {
        "app_name":        req.app_name,
        "tier":            req.tier,
        "selfheal_token":  token,
        "token_prefix":    token[:12] + "…",
        "message":         (
            f"Set SELFHEAL_TOKEN={token} in your app's environment. "
            "Keep this secret — it authenticates your app's telemetry."
        ),
    }


@router.get("/apps", tags=["integration"])
async def list_apps() -> list[dict[str, Any]]:
    """List all registered apps (for ops dashboards — no auth required)."""
    store = get_token_store()
    return await store.list_apps()


@router.post("/apps/{app_name}/rotate-token", tags=["integration"])
async def rotate_token(app_name: str) -> dict[str, str]:
    """Generate a new SELFHEAL_TOKEN for an app, invalidating the old one."""
    store     = get_token_store()
    new_token = await store.rotate_token(app_name)
    return {
        "app_name":       app_name,
        "selfheal_token": new_token,
        "message":        "Old token invalidated. Update SELFHEAL_TOKEN in your environment.",
    }


# ──────────────────────────────────────────────────────────────────────────────
# Beacon.js CDN endpoint
# ──────────────────────────────────────────────────────────────────────────────

@router.get("/sdk/beacon.js", include_in_schema=False)
def serve_beacon() -> Response:
    """Serve the NEXUS browser beacon SDK as a JavaScript file."""
    if not _BEACON_JS_PATH.exists():
        raise HTTPException(status_code=404, detail="beacon.js not found")
    content = _BEACON_JS_PATH.read_text(encoding="utf-8")
    return Response(
        content    = content,
        media_type = "application/javascript",
        headers    = {"Cache-Control": "public, max-age=3600"},
    )


# ──────────────────────────────────────────────────────────────────────────────
# SDK payloads
# ──────────────────────────────────────────────────────────────────────────────

class GenericEventPayload(BaseModel):
    type:    str
    message: str | None = None
    data:    dict[str, Any] = Field(default_factory=dict)
    ts:      float | None = None


class HeartbeatPayload(BaseModel):
    memory_mb:   float | None = None
    cpu_pct:     float | None = None
    uptime_s:    float | None = None
    error_rate:  float | None = None
    rps:         float | None = None
    version:     str | None   = None
    ts:          float | None  = None


class RouteErrorPayload(BaseModel):
    route:       str
    method:      str = "GET"
    status_code: int
    duration_ms: float | None = None
    error_msg:   str | None   = None
    ts:          float | None  = None


class QueryPayload(BaseModel):
    sql_preview:    str
    duration_ms:    float
    tables:         list[str]       = Field(default_factory=list)
    slow:           bool            = False
    ts:             float | None = None


class FrontendPayload(BaseModel):
    type:    str                       # js_error | web_vital | failed_fetch | failed_xhr | unhandled_rejection
    message: str | None = None
    stack:   str | None = None
    source:  str | None = None
    url:     str | None = None
    name:    str | None = None      # Web Vital name (LCP, CLS, FID)
    value:   float | None = None    # Web Vital value
    status:  int | None  = None     # HTTP status for failed requests
    ts:      float | None = None


# ──────────────────────────────────────────────────────────────────────────────
# Ingest endpoints
# ──────────────────────────────────────────────────────────────────────────────

def _ts_to_iso(ts: float | None) -> str:
    if ts is None:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()


async def _publish_to_nats(
    app_name:   str,
    event_type: str,
    payload:    dict[str, Any],
) -> None:
    """
    Translate an SDK payload into an IncidentEvent and publish to NATS.
    Publishes to nexus.incidents.sdk.<event_type> — exactly what the demo
    orchestrator subscribes to (nexus.incidents.sdk.*).
    """
    # Event types that should NOT trigger the orchestrator
    _ignore = {"heartbeat", "deploy_event", "healing_completed"}
    if event_type in _ignore:
        return

    try:
        from nexus.bus.incident_event import AgentType, IncidentEvent, Severity, SignalType

        _type_map = {
            # Standard SDK events
            "route_error":             SignalType.HIGH_ERROR_RATE,
            "js_error":                SignalType.HIGH_ERROR_RATE,
            "unhandled_rejection":     SignalType.HIGH_ERROR_RATE,
            "failed_fetch":            SignalType.HIGH_ERROR_RATE,
            "failed_xhr":              SignalType.HIGH_ERROR_RATE,
            "db_query":                SignalType.SLOW_QUERY_DETECTED,
            "function_error":          SignalType.HIGH_ERROR_RATE,
            "web_vital":               SignalType.THRESHOLD_BREACH,
            # Demo injection events
            "db_spike_injected":       SignalType.SLOW_QUERY_DETECTED,
            "error_rate_injected":     SignalType.HIGH_ERROR_RATE,
            "slow_query_injected":     SignalType.SLOW_QUERY_DETECTED,
            "oom_injected":            SignalType.THRESHOLD_BREACH,
            "dns_error_injected":      SignalType.HIGH_ERROR_RATE,
            "config_drift_detected":   SignalType.ENV_CONTRACT_VIOLATION,
            "bad_config":              SignalType.ENV_CONTRACT_VIOLATION,
            "env_contract_violation":  SignalType.ENV_CONTRACT_VIOLATION,
            "secret_committed":        SignalType.ENV_CONTRACT_VIOLATION,
            "code_quality_warning":    SignalType.HIGH_ERROR_RATE,
        }

        signal = _type_map.get(event_type)
        if signal is None:
            return   # heartbeats and unknown types are not incident events


        event = IncidentEvent(
            agent         = AgentType.ORCHESTRATOR,  # SDK acts as a virtual "app agent"
            signal_type   = signal,
            severity      = Severity.WARNING,
            namespace     = "sdk",
            resource_name = app_name,
            resource_kind = "Application",
            context       = {"sdk_event_type": event_type, "app": app_name, **payload},
        )

        # Publish to nexus.incidents.sdk.<type> via lazy raw NATS connection.
        # This subject matches the demo orchestrator subscription: nexus.incidents.sdk.*
        nc = await _get_nats()
        if nc and not nc.is_closed:
            subject = f"nexus.incidents.sdk.{event_type}"
            await nc.publish(subject, event.model_dump_json().encode())
            logger.info(f"[SDKIngest] → NATS {subject}")
        else:
            logger.warning(f"[SDKIngest] NATS unavailable — {event_type} dropped")
    except Exception as exc:
        logger.warning(f"[SDKIngest] publish error: {exc}")





@router.post("/sdk/event", tags=["integration"])
async def sdk_event(
    payload:  GenericEventPayload,
    app_name: str = Depends(_get_app_name),
) -> dict[str, str]:
    """Accept a generic SDK event from any SDK client."""
    await _publish_to_nats(app_name, payload.type, {
        "message": payload.message, **payload.data,
        "ts": _ts_to_iso(payload.ts),
    })
    logger.debug(f"[SDKIngest] event: app={app_name} type={payload.type}")
    return {"status": "accepted"}


@router.post("/sdk/heartbeat", tags=["integration"])
async def sdk_heartbeat(
    payload:  HeartbeatPayload,
    app_name: str = Depends(_get_app_name),
) -> dict[str, str]:
    """Accept a heartbeat / health snapshot from the SDK."""
    data = payload.model_dump(exclude_none=True)
    data["ts"] = _ts_to_iso(payload.ts)

    # Trigger anomaly if error_rate is elevated
    if payload.error_rate and payload.error_rate > 0.05:
        await _publish_to_nats(app_name, "route_error", {
            "route": "*", "method": "*",
            "status_code": 500, "error_rate": payload.error_rate,
            "ts": data["ts"],
        })

    logger.debug(f"[SDKIngest] heartbeat: app={app_name} rps={payload.rps}")
    return {"status": "accepted"}


@router.post("/sdk/route-error", tags=["integration"])
async def sdk_route_error(
    payload:  RouteErrorPayload,
    app_name: str = Depends(_get_app_name),
) -> dict[str, str]:
    """Accept a per-route HTTP error from backend SDK middleware."""
    await _publish_to_nats(app_name, "route_error", {
        "route":       payload.route,
        "method":      payload.method,
        "status_code": payload.status_code,
        "duration_ms": payload.duration_ms,
        "error_msg":   payload.error_msg,
        "ts":          _ts_to_iso(payload.ts),
    })
    logger.debug(
        f"[SDKIngest] route-error: app={app_name} "
        f"route={payload.route} status={payload.status_code}"
    )
    return {"status": "accepted"}


@router.post("/sdk/query", tags=["integration"])
async def sdk_query(
    payload:  QueryPayload,
    app_name: str = Depends(_get_app_name),
) -> dict[str, str]:
    """Accept a DB query report from SelfHeal.wrapDB()."""
    await _publish_to_nats(app_name, "db_query", {
        "sql_preview": payload.sql_preview,
        "duration_ms": payload.duration_ms,
        "tables":      payload.tables,
        "slow":        payload.slow or payload.duration_ms > 200,
        "ts":          _ts_to_iso(payload.ts),
    })
    logger.debug(
        f"[SDKIngest] query: app={app_name} "
        f"tables={payload.tables} {payload.duration_ms:.0f}ms"
    )
    return {"status": "accepted"}


@router.post("/sdk/frontend", tags=["integration"])
async def sdk_frontend(
    payload:  FrontendPayload,
    app_name: str = Depends(_get_app_name),
) -> dict[str, str]:
    """Accept a browser beacon payload (JS errors, Web Vitals, failed requests)."""
    data = payload.model_dump(exclude_none=True)
    data["ts"] = _ts_to_iso(payload.ts)
    await _publish_to_nats(app_name, payload.type, data)
    logger.debug(f"[SDKIngest] frontend: app={app_name} type={payload.type}")
    return {"status": "accepted"}
