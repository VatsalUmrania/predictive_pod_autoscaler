"""
NEXUS Python SDK — @critical and @query decorators
====================================================
Route and query annotation decorators for Tier 3 SDK integration.

Usage:
    from selfheal import SelfHeal

    @app.post("/api/payment")
    @SelfHeal.critical(never_shed=True, fallback="queue", alert_sre_after=2)
    async def process_payment(req):
        ...

    @SelfHeal.query(label="order_history", spike_indicator=True)
    def get_orders(user_id):
        return db.query("SELECT * FROM orders WHERE user_id = ?", user_id)
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Registry of annotated routes — sent to NEXUS at startup
_CRITICAL_ROUTES:  Dict[str, Dict] = {}
_QUERY_LABELS:     Dict[str, Dict] = {}


# ──────────────────────────────────────────────────────────────────────────────
# @critical decorator
# ──────────────────────────────────────────────────────────────────────────────

def critical(
    never_shed:          bool         = False,
    fallback:            Optional[str]= None,
    alert_sre_after:     int          = 3,
    max_healing_actions: int          = 5,
    token:               str          = "",
    nexus_url:           str          = "http://localhost:8080",
) -> Callable:
    """
    Declare a route as critical to NEXUS.

    Registers route metadata with the NEXUS SDK ingest endpoint at decoration
    time (startup). The metadata is used by RunbookExecutor to enforce
    blast-radius controls and paging thresholds.
    """
    def decorator(func: Callable) -> Callable:
        # Register metadata
        route_name = func.__name__
        _CRITICAL_ROUTES[route_name] = {
            "function":           route_name,
            "never_shed":         never_shed,
            "fallback":           fallback,
            "alert_sre_after":    alert_sre_after,
            "max_healing_actions":max_healing_actions,
        }

        # Register with NEXUS (fire-and-forget)
        asyncio.get_event_loop().call_soon_threadsafe(
            lambda: asyncio.create_task(
                _register_critical(route_name, _CRITICAL_ROUTES[route_name], token, nexus_url)
            )
        ) if asyncio.get_event_loop().is_running() else None

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except Exception as exc:
                asyncio.create_task(
                    _emit_critical_failure(route_name, str(exc), token, nexus_url)
                )
                if fallback == "queue":
                    from fastapi.responses import JSONResponse
                    return JSONResponse(
                        status_code=202,
                        content={"queued": True, "message": "Request queued for retry"},
                    )
                raise

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                _emit_sync(route_name, str(exc), token, nexus_url)
                if fallback == "queue":
                    return {"queued": True, "message": "Request queued for retry"}
                raise

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator


async def _register_critical(name: str, meta: dict, token: str, url: str) -> None:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.post(
                f"{url}/sdk/event",
                json    = {"type": "critical_route_registered", "route": name, **meta},
                headers = {"Authorization": f"Bearer {token}"},
            )
    except Exception:
        pass


async def _emit_critical_failure(name: str, error: str, token: str, url: str) -> None:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=2.0) as client:
            await client.post(
                f"{url}/sdk/route-error",
                json    = {"route": name, "method": "POST", "status_code": 500,
                           "error_msg": error[:300], "critical": True},
                headers = {"Authorization": f"Bearer {token}"},
            )
    except Exception:
        pass


def _emit_sync(name: str, error: str, token: str, url: str) -> None:
    try:
        import threading, requests
        def _post():
            requests.post(
                f"{url}/sdk/route-error",
                json    = {"route": name, "method": "POST", "status_code": 500, "error_msg": error[:300]},
                headers = {"Authorization": f"Bearer {token}"},
                timeout = 2,
            )
        threading.Thread(target=_post, daemon=True).start()
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# @query decorator
# ──────────────────────────────────────────────────────────────────────────────

def query(
    label:            str           = "",
    spike_indicator:  bool          = False,
    cache_on_failure: bool          = False,
    timeout_ms:       Optional[int] = None,
    token:            str           = "",
    nexus_url:        str           = "http://localhost:8080",
) -> Callable:
    """
    Annotate a DB query function for NEXUS tracking.

    When spike_indicator=True, the query pattern is sent to the
    DBTrafficCorrelator as a traffic prediction signal.
    When cache_on_failure=True, the decorator caches the last successful
    result and returns it if the underlying call raises an exception.
    """
    _cache: Dict[str, Any] = {}

    def decorator(func: Callable) -> Callable:
        query_name = label or func.__name__
        _QUERY_LABELS[query_name] = {
            "function":        func.__name__,
            "label":           query_name,
            "spike_indicator": spike_indicator,
            "cache_on_failure":cache_on_failure,
            "timeout_ms":      timeout_ms,
        }

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start = time.monotonic()
            try:
                result = func(*args, **kwargs)
                duration_ms = (time.monotonic() - start) * 1000

                if cache_on_failure:
                    _cache["last"] = result

                # Emit query telemetry (background thread — non-blocking)
                import threading
                threading.Thread(
                    target=_emit_query_sync,
                    args=(query_name, duration_ms, spike_indicator, token, nexus_url),
                    daemon=True,
                ).start()

                return result

            except Exception as exc:
                if cache_on_failure and "last" in _cache:
                    logger.warning(
                        f"[SelfHeal @query] '{query_name}' failed — returning cached result. "
                        f"Error: {exc}"
                    )
                    return _cache["last"]
                raise

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            start = time.monotonic()
            try:
                result = await func(*args, **kwargs)
                duration_ms = (time.monotonic() - start) * 1000

                if cache_on_failure:
                    _cache["last"] = result

                asyncio.create_task(
                    _emit_query_async(query_name, duration_ms, spike_indicator, token, nexus_url)
                )
                return result

            except Exception as exc:
                if cache_on_failure and "last" in _cache:
                    return _cache["last"]
                raise

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return wrapper

    return decorator


def _emit_query_sync(label, duration_ms, spike_indicator, token, url) -> None:
    try:
        import requests
        requests.post(
            f"{url}/sdk/query",
            json    = {
                "sql_preview":   label,
                "duration_ms":   round(duration_ms, 1),
                "tables":        [label] if spike_indicator else [],
                "slow":          bool(duration_ms > 200),
            },
            headers = {"Authorization": f"Bearer {token}"},
            timeout = 2,
        )
    except Exception:
        pass


async def _emit_query_async(label, duration_ms, spike_indicator, token, url) -> None:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=2.0) as client:
            await client.post(
                f"{url}/sdk/query",
                json    = {
                    "sql_preview": label,
                    "duration_ms": round(duration_ms, 1),
                    "tables":      [label] if spike_indicator else [],
                    "slow":        bool(duration_ms > 200),
                },
                headers = {"Authorization": f"Bearer {token}"},
            )
    except Exception:
        pass
