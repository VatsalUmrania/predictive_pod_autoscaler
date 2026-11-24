"""
NEXUS Python SDK — @critical and @query decorators
====================================================
Route and query annotation decorators for Tier 3 SDK integration.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable

from nexus.sdk.python.query_decorator import _QUERY_LABELS, query

logger = logging.getLogger(__name__)

# Registry of annotated routes — sent to NEXUS at startup
_CRITICAL_ROUTES: dict[str, dict] = {}

__all__ = [
    "critical",
    "query",
    "_CRITICAL_ROUTES",
    "_QUERY_LABELS",
]


def critical(
    never_shed: bool = False,
    fallback: str | None = None,
    alert_sre_after: int = 3,
    max_healing_actions: int = 5,
    token: str = "",
    nexus_url: str = "http://localhost:8080",
) -> Callable:
    """Declare a route as critical to NEXUS."""

    def decorator(func: Callable) -> Callable:
        route_name = func.__name__
        _CRITICAL_ROUTES[route_name] = {
            "function": route_name,
            "never_shed": never_shed,
            "fallback": fallback,
            "alert_sre_after": alert_sre_after,
            "max_healing_actions": max_healing_actions,
        }

        try:
            loop = asyncio.get_running_loop()
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(
                    _register_critical(
                        route_name, _CRITICAL_ROUTES[route_name], token, nexus_url
                    )
                )
            )
        except RuntimeError:
            pass

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
                json={"type": "critical_route_registered", "route": name, **meta},
                headers={"Authorization": f"Bearer {token}"},
            )
    except Exception:
        pass


async def _emit_critical_failure(name: str, error: str, token: str, url: str) -> None:
    try:
        import httpx

        async with httpx.AsyncClient(timeout=2.0) as client:
            await client.post(
                f"{url}/sdk/route-error",
                json={
                    "route": name,
                    "method": "POST",
                    "status_code": 500,
                    "error_msg": error[:300],
                    "critical": True,
                },
                headers={"Authorization": f"Bearer {token}"},
            )
    except Exception:
        pass


def _emit_sync(name: str, error: str, token: str, url: str) -> None:
    try:
        import threading

        import requests

        def _post():
            requests.post(
                f"{url}/sdk/route-error",
                json={
                    "route": name,
                    "method": "POST",
                    "status_code": 500,
                    "error_msg": error[:300],
                },
                headers={"Authorization": f"Bearer {token}"},
                timeout=2,
            )

        threading.Thread(target=_post, daemon=True).start()
    except Exception:
        pass
