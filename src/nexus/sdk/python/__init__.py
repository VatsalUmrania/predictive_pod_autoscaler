"""
NEXUS Python SDK — Public Entry Point
========================================
Provides the SelfHeal class that developers add to their Python backend
(FastAPI, Django, Flask, Starlette, or any ASGI/WSGI app) in ~2 lines.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from nexus.sdk.python.db_wrapper import _DBWrapper

logger = logging.getLogger(__name__)
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


class _SelfHeal:
    """Developer-facing SDK entry point."""

    def __init__(self) -> None:
        self._token: str | None = None
        self._nexus_url: str = os.getenv("NEXUS_API_URL", "http://localhost:8080")
        self._app_name: str | None = None
        self._initialized = False
        self.wrapDB = self.wrap_db

    def init(
        self,
        app: Any,
        token: str | None = None,
        nexus_url: str | None = None,
        wsgi: bool = False,
        slow_threshold_ms: int = 500,
    ) -> Any:
        """Wrap an ASGI (or WSGI) app with NEXUS telemetry middleware."""
        self._token = token or os.getenv("SELFHEAL_TOKEN", "")
        self._nexus_url = nexus_url or self._nexus_url

        if not self._token:
            logger.warning(
                "[SelfHeal] No SELFHEAL_TOKEN provided. "
                "Set SELFHEAL_TOKEN or call SelfHeal.init(token=...)."
            )

        mw = _get_middleware()
        wrapped = mw(
            app=app,
            token=self._token,
            nexus_url=self._nexus_url,
            wsgi=wsgi,
            slow_ms=slow_threshold_ms,
        )

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
        never_shed: bool = False,
        fallback: str | None = None,
        alert_sre_after: int = 3,
        max_healing_actions: int = 5,
    ):
        """Route-level annotation for critical endpoints."""
        return _get_decorators().critical(
            never_shed=never_shed,
            fallback=fallback,
            alert_sre_after=alert_sre_after,
            max_healing_actions=max_healing_actions,
            token=self._token,
            nexus_url=self._nexus_url,
        )

    def query(
        self,
        label: str = "",
        spike_indicator: bool = False,
        cache_on_failure: bool = False,
        timeout_ms: int | None = None,
    ):
        """DB query annotation — marks a query function for NEXUS tracking."""
        return _get_decorators().query(
            label=label,
            spike_indicator=spike_indicator,
            cache_on_failure=cache_on_failure,
            timeout_ms=timeout_ms,
            token=self._token,
            nexus_url=self._nexus_url,
        )

    async def send_event(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Manually emit a custom event to NEXUS."""
        try:
            import httpx

            async with httpx.AsyncClient(timeout=3.0) as client:
                await client.post(
                    f"{self._nexus_url}/sdk/event",
                    json={"type": event_type, **(data or {})},
                    headers={"Authorization": f"Bearer {self._token}"},
                )
        except Exception as exc:
            logger.debug(f"[SelfHeal] send_event failed: {exc}")

    def wrap_db(
        self,
        engine_or_pool: Any,
        slow_ms: int = 200,
        track_tables: bool = False,
        label: str = "",
    ) -> Any:
        """Wrap a database engine or connection pool with NEXUS telemetry."""
        return _DBWrapper(
            wrapped=engine_or_pool,
            token=self._token or "",
            nexus_url=self._nexus_url,
            slow_ms=slow_ms,
            track_tables=track_tables,
            label=label or type(engine_or_pool).__name__,
        )


SelfHeal = _SelfHeal()
__all__ = ["SelfHeal", "_SelfHeal", "_DBWrapper"]
