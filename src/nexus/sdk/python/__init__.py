"""
NEXUS Python SDK — Public Entry Point
========================================
Provides the SelfHeal class that developers add to their Python backend
(FastAPI, Django, Flask, Starlette, or any ASGI/WSGI app) in ~2 lines.

Usage:
    from selfheal import SelfHeal

    # FastAPI / Starlette (ASGI)
    SelfHeal.init(app, token=os.environ["SELFHEAL_TOKEN"])

    # Django / Flask (WSGI) — wraps as WSGI middleware
    SelfHeal.init(app, token=os.environ["SELFHEAL_TOKEN"], wsgi=True)

What it does:
    - Wraps your app with SelfHealMiddleware (ASGI or WSGI)
    - Captures 5xx routes, unhandled exceptions, and slow responses
    - Sends structured telemetry to the NEXUS Status API (/sdk/route-error)
    - Non-blocking: uses a background asyncio task / thread

The token (SELFHEAL_TOKEN) is issued by NEXUS when you run:
    POST /apps/register   { "app_name": "my-service", "tier": "production" }
or by committing selfheal.yaml to your repo (auto-issued by GitAgent on push).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Lazy imports (so sdk can be installed without nexus core)
# ──────────────────────────────────────────────────────────────────────────────

_middleware_cls = None
_decorators_mod = None


def _get_middleware():
    global _middleware_cls
    if _middleware_cls is None:
        from nexus.sdk.python.middleware import SelfHealMiddleware
        _middleware_cls = SelfHealMiddleware
    return _middleware_cls


def _get_decorators():
    global _decorators_mod
    if _decorators_mod is None:
        from nexus.sdk.python import decorators
        _decorators_mod = decorators
    return _decorators_mod


# ──────────────────────────────────────────────────────────────────────────────
# Public SelfHeal class
# ──────────────────────────────────────────────────────────────────────────────

class _SelfHeal:
    """
    Developer-facing SDK entry point.

    Methods:
        init(app, token, ...)      — Wrap app with middleware (call at startup)
        critical(...)              — Decorator for critical route annotation
        query(...)                 — Decorator for DB query annotation
        send_event(type, data)     — Manually emit a custom event
    """

    def __init__(self) -> None:
        self._token:    Optional[str] = None
        self._nexus_url: str = os.getenv("NEXUS_API_URL", "http://localhost:8080")
        self._app_name: Optional[str] = None
        self._initialized = False

    def init(
        self,
        app:       Any,
        token:     Optional[str]  = None,
        nexus_url: Optional[str]  = None,
        wsgi:      bool           = False,
        slow_threshold_ms: int    = 500,
    ) -> Any:
        """
        Wrap an ASGI (or WSGI) app with NEXUS telemetry middleware.

        Args:
            app:              Your FastAPI / Starlette / Django / Flask app.
            token:            SELFHEAL_TOKEN from NEXUS (or set SELFHEAL_TOKEN env var).
            nexus_url:        Override NEXUS API URL (default: NEXUS_API_URL env or http://localhost:8080).
            wsgi:             Set True for WSGI apps (Django, Flask, Gunicorn).
            slow_threshold_ms: Emit a slow-response event if request exceeds this (ms).

        Returns:
            The app wrapped with SelfHealMiddleware (same type as input for ASGI).
        """
        self._token     = token or os.getenv("SELFHEAL_TOKEN", "")
        self._nexus_url = nexus_url or self._nexus_url

        if not self._token:
            logger.warning(
                "[SelfHeal] No SELFHEAL_TOKEN provided. "
                "Set SELFHEAL_TOKEN or call SelfHeal.init(token=...)."
            )

        MW = _get_middleware()
        wrapped = MW(
            app             = app,
            token           = self._token,
            nexus_url       = self._nexus_url,
            wsgi            = wsgi,
            slow_ms         = slow_threshold_ms,
        )

        # Stash app name from the wrapped object if available
        if hasattr(app, "title"):
            self._app_name = getattr(app, "title", None)

        self._initialized = True
        logger.info(
            f"[SelfHeal] Initialized — nexus_url={self._nexus_url} "
            f"token={self._token[:8] + '…' if self._token else 'MISSING'}"
        )
        return wrapped

    def critical(
        self,
        never_shed:          bool         = False,
        fallback:            Optional[str]= None,
        alert_sre_after:     int          = 3,
        max_healing_actions: int          = 5,
    ):
        """
        Route-level annotation for critical endpoints.

        Args:
            never_shed:          Never drop this route under load shedding.
            fallback:            "queue" | "cache" | None — what to do if failing.
            alert_sre_after:     Page a human after N failed healing attempts.
            max_healing_actions: NEXUS tries at most N healing actions before stopping.

        Usage:
            @app.post("/api/payment")
            @SelfHeal.critical(never_shed=True, fallback="queue", alert_sre_after=2)
            async def process_payment(req):
                ...
        """
        return _get_decorators().critical(
            never_shed          = never_shed,
            fallback            = fallback,
            alert_sre_after     = alert_sre_after,
            max_healing_actions = max_healing_actions,
            token               = self._token,
            nexus_url           = self._nexus_url,
        )

    def query(
        self,
        label:            str           = "",
        spike_indicator:  bool          = False,
        cache_on_failure: bool          = False,
        timeout_ms:       Optional[int] = None,
    ):
        """
        DB query annotation — marks a query function for NEXUS tracking.

        Args:
            label:            Human-readable name for this query (for dashboard display).
            spike_indicator:  Use this query as a traffic spike predictor.
            cache_on_failure: Return cached result if DB is down.
            timeout_ms:       Emit an alert if this query exceeds N ms.

        Usage:
            @SelfHeal.query(label="user_order_history", spike_indicator=True)
            def get_orders(user_id):
                return db.query("SELECT * FROM orders WHERE user_id = ?", user_id)
        """
        return _get_decorators().query(
            label            = label,
            spike_indicator  = spike_indicator,
            cache_on_failure = cache_on_failure,
            timeout_ms       = timeout_ms,
            token            = self._token,
            nexus_url        = self._nexus_url,
        )

    async def send_event(self, event_type: str, data: dict = None) -> None:
        """Manually emit a custom event to NEXUS."""
        try:
            import httpx
            async with httpx.AsyncClient(timeout=3.0) as client:
                await client.post(
                    f"{self._nexus_url}/sdk/event",
                    json    = {"type": event_type, **(data or {})},
                    headers = {"Authorization": f"Bearer {self._token}"},
                )
        except Exception as exc:
            logger.debug(f"[SelfHeal] send_event failed: {exc}")


# Module-level singleton — mirrors the JS SDK pattern
SelfHeal = _SelfHeal()
