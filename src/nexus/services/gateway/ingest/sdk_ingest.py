"""NEXUS SDK Ingest Router - FastAPI router that receives telemetry from SDK clients."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Response

from nexus.services.gateway.auth.token_store import get_token_store
from nexus.services.gateway.ingest.sdk_payloads import (
    FrontendPayload,
    GenericEventPayload,
    HeartbeatPayload,
    QueryPayload,
    RegisterRequest,
    RouteErrorPayload,
)
from nexus.services.gateway.ingest.sdk_publisher import (
    get_nats,
    publish_sdk_to_nats,
    ts_to_iso,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_BEACON_JS_PATH = Path(__file__).parent.parent / "sdk" / "js" / "beacon.js"

# Re-exports for backward compatibility
_publish_to_nats = publish_sdk_to_nats
_get_nats = get_nats
_ts_to_iso = ts_to_iso


async def _get_app_name(authorization: str | None = Header(None)) -> str:
    """Validate Bearer token and return app_name."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401, detail="Missing Authorization: Bearer <token>"
        )
    token = authorization[len("Bearer ") :]
    store = get_token_store()
    app_name = await store.validate_token(token)
    if not app_name:
        raise HTTPException(status_code=401, detail="Invalid or unknown SELFHEAL_TOKEN")
    return app_name


@router.post("/apps/register", tags=["integration"])
async def register_app(req: RegisterRequest) -> dict[str, str]:
    """Register a new application with NEXUS and receive a SELFHEAL_TOKEN."""
    store = get_token_store()
    token = await store.register_app(req.app_name, tier=req.tier)
    logger.info(f"[SDKIngest] App registered: {req.app_name} (tier={req.tier})")
    return {
        "app_name": req.app_name,
        "tier": req.tier,
        "selfheal_token": token,
        "token_prefix": token[:12] + "…",
        "message": f"Set SELFHEAL_TOKEN={token} in your app's environment.",
    }


@router.get("/apps", tags=["integration"])
async def list_apps() -> list[dict[str, Any]]:
    """List all registered apps."""
    return await get_token_store().list_apps()


@router.post("/apps/{app_name}/rotate-token", tags=["integration"])
async def rotate_token(app_name: str) -> dict[str, str]:
    """Generate a new SELFHEAL_TOKEN for an app, invalidating the old one."""
    new_token = await get_token_store().rotate_token(app_name)
    return {
        "app_name": app_name,
        "selfheal_token": new_token,
        "message": "Old token invalidated. Update SELFHEAL_TOKEN in your environment.",
    }


@router.get("/sdk/beacon.js", include_in_schema=False)
def serve_beacon() -> Response:
    """Serve the NEXUS browser beacon SDK as a JavaScript file."""
    if not _BEACON_JS_PATH.exists():
        raise HTTPException(status_code=404, detail="beacon.js not found")
    content = _BEACON_JS_PATH.read_text(encoding="utf-8")
    return Response(
        content=content,
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.post("/sdk/event", tags=["integration"])
async def sdk_event(
    payload: GenericEventPayload,
    app_name: str = Depends(_get_app_name),
) -> dict[str, str]:
    """Accept a generic SDK event from any SDK client."""
    await publish_sdk_to_nats(
        app_name,
        payload.type,
        {"message": payload.message, **payload.data, "ts": ts_to_iso(payload.ts)},
    )
    logger.debug(f"[SDKIngest] event: app={app_name} type={payload.type}")
    return {"status": "accepted"}


@router.post("/sdk/heartbeat", tags=["integration"])
async def sdk_heartbeat(
    payload: HeartbeatPayload,
    app_name: str = Depends(_get_app_name),
) -> dict[str, str]:
    """Accept a heartbeat / health snapshot from the SDK."""
    data = payload.model_dump(exclude_none=True)
    data["ts"] = ts_to_iso(payload.ts)

    if payload.error_rate and payload.error_rate > 0.05:
        await publish_sdk_to_nats(
            app_name,
            "route_error",
            {
                "route": "*",
                "method": "*",
                "status_code": 500,
                "error_rate": payload.error_rate,
                "ts": data["ts"],
            },
        )

    logger.debug(f"[SDKIngest] heartbeat: app={app_name} rps={payload.rps}")
    return {"status": "accepted"}


@router.post("/sdk/route-error", tags=["integration"])
async def sdk_route_error(
    payload: RouteErrorPayload,
    app_name: str = Depends(_get_app_name),
) -> dict[str, str]:
    """Accept a per-route HTTP error from backend SDK middleware."""
    await publish_sdk_to_nats(
        app_name,
        "route_error",
        {
            "route": payload.route,
            "method": payload.method,
            "status_code": payload.status_code,
            "duration_ms": payload.duration_ms,
            "error_msg": payload.error_msg,
            "ts": ts_to_iso(payload.ts),
        },
    )
    logger.debug(f"[SDKIngest] route-error: app={app_name} route={payload.route}")
    return {"status": "accepted"}


@router.post("/sdk/query", tags=["integration"])
async def sdk_query(
    payload: QueryPayload,
    app_name: str = Depends(_get_app_name),
) -> dict[str, str]:
    """Accept a DB query report from SelfHeal.wrapDB()."""
    await publish_sdk_to_nats(
        app_name,
        "db_query",
        {
            "sql_preview": payload.sql_preview,
            "duration_ms": payload.duration_ms,
            "tables": payload.tables,
            "slow": payload.slow or payload.duration_ms > 200,
            "ts": ts_to_iso(payload.ts),
        },
    )
    logger.debug(f"[SDKIngest] query: app={app_name} {payload.duration_ms:.0f}ms")
    return {"status": "accepted"}


@router.post("/sdk/frontend", tags=["integration"])
async def sdk_frontend(
    payload: FrontendPayload,
    app_name: str = Depends(_get_app_name),
) -> dict[str, str]:
    """Accept a browser beacon payload (JS errors, Web Vitals, failed requests)."""
    data = payload.model_dump(exclude_none=True)
    data["ts"] = ts_to_iso(payload.ts)
    await publish_sdk_to_nats(app_name, payload.type, data)
    logger.debug(f"[SDKIngest] frontend: app={app_name} type={payload.type}")
    return {"status": "accepted"}
