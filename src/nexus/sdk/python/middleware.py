"""
NEXUS ASGI/WSGI Middleware
===========================
Non-blocking middleware that captures every HTTP 5xx / slow response
and fires it to the NEXUS Status API as an SDK route-error event.

Usage (FastAPI / Starlette):
    from nexus.sdk.python.middleware import SelfHealMiddleware
    app.add_middleware(SelfHealMiddleware,
                       token=os.environ["SELFHEAL_TOKEN"],
                       nexus_url="http://localhost:8080")

Usage (Django / Flask WSGI):
    app = SelfHealMiddleware(app, token="sh_xxx", wsgi=True)

Design:
    All network I/O is fire-and-forget (asyncio.create_task / daemon thread).
    The request path is NEVER blocked.
    If NEXUS is unreachable, the middleware silently skips emission.
"""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class SelfHealMiddleware:
    """
    Drop-in ASGI middleware for FastAPI / Starlette.
    Detects HTTP 5xx responses and slow requests, emitting them to NEXUS.

    Add to FastAPI:
        app.add_middleware(SelfHealMiddleware, token="sh_xxx", nexus_url="http://localhost:8080")
    """

    def __init__(
        self,
        app:       Any,
        token:     str  = "",
        nexus_url: str  = "http://localhost:8080",
        wsgi:      bool = False,
        slow_ms:   int  = 500,
    ) -> None:
        self.app      = app
        self._token   = token
        self._url     = nexus_url.rstrip("/")
        self._wsgi    = wsgi
        self._slow_ms = slow_ms

    # ── ASGI interface ────────────────────────────────────────────────────────

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        start      = time.monotonic()
        status_ref = [200]
        exc_ref: list = [None]

        async def _send_wrapper(message: dict) -> None:
            if message.get("type") == "http.response.start":
                status_ref[0] = message.get("status", 200)
            await send(message)

        try:
            await self.app(scope, receive, _send_wrapper)
        except Exception as exc:
            exc_ref[0] = exc
            raise
        finally:
            duration_ms = (time.monotonic() - start) * 1000
            status      = status_ref[0]
            exc         = exc_ref[0]

            if status >= 500 or exc is not None or duration_ms > self._slow_ms:
                path   = scope.get("path", "/unknown")
                method = scope.get("method", "GET")
                err_msg = (
                    traceback.format_exception_only(type(exc), exc)[-1].strip()
                    if exc else None
                )
                # Fire-and-forget — does not block the response
                try:
                    asyncio.get_event_loop().create_task(
                        self._emit(path, method, status if not exc else 500,
                                   duration_ms, err_msg)
                    )
                except RuntimeError:
                    pass   # No running event loop (test context)

    # ── Emission ──────────────────────────────────────────────────────────────

    async def _emit(
        self,
        route:       str,
        method:      str,
        status_code: int,
        duration_ms: float,
        error_msg:   Optional[str] = None,
    ) -> None:
        if not self._token:
            return
        try:
            import httpx
            payload = {
                "route":       route,
                "method":      method,
                "status_code": status_code,
                "duration_ms": round(duration_ms, 1),
            }
            if error_msg:
                payload["error_msg"] = error_msg[:400]
            async with httpx.AsyncClient(timeout=2.0) as client:
                await client.post(
                    f"{self._url}/sdk/route-error",
                    json    = payload,
                    headers = {"Authorization": f"Bearer {self._token}"},
                )
        except Exception as exc:
            logger.debug(f"[SelfHealMiddleware] emit skipped: {exc}")
