"""
@query decorator and telemetry emission for NEXUS Python SDK.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

_QUERY_LABELS: dict[str, dict] = {}


def query(
    label: str = "",
    spike_indicator: bool = False,
    cache_on_failure: bool = False,
    timeout_ms: int | None = None,
    token: str = "",
    nexus_url: str = "http://localhost:8080",
) -> Callable:
    """Annotate a DB query function for NEXUS tracking."""
    _cache: dict[str, Any] = {}

    def decorator(func: Callable) -> Callable:
        query_name = label or func.__name__
        _QUERY_LABELS[query_name] = {
            "function": func.__name__,
            "label": query_name,
            "spike_indicator": spike_indicator,
            "cache_on_failure": cache_on_failure,
            "timeout_ms": timeout_ms,
        }

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start = time.monotonic()
            try:
                result = func(*args, **kwargs)
                duration_ms = (time.monotonic() - start) * 1000

                if cache_on_failure:
                    _cache["last"] = result

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
                        f"[SelfHeal @query] '{query_name}' failed — returning cached result. Error: {exc}"
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
                    _emit_query_async(
                        query_name, duration_ms, spike_indicator, token, nexus_url
                    )
                )
                return result

            except Exception:
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
            json={
                "sql_preview": label,
                "duration_ms": round(duration_ms, 1),
                "tables": [label] if spike_indicator else [],
                "slow": bool(duration_ms > 200),
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=2,
        )
    except Exception:
        pass


async def _emit_query_async(label, duration_ms, spike_indicator, token, url) -> None:
    try:
        import httpx

        async with httpx.AsyncClient(timeout=2.0) as client:
            await client.post(
                f"{url}/sdk/query",
                json={
                    "sql_preview": label,
                    "duration_ms": round(duration_ms, 1),
                    "tables": [label] if spike_indicator else [],
                    "slow": bool(duration_ms > 200),
                },
                headers={"Authorization": f"Bearer {token}"},
            )
    except Exception:
        pass
