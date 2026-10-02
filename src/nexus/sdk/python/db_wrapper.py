"""
NEXUS Python SDK — Database Proxy Wrapper
==========================================
Transparent proxy around any DB engine or connection pool that intercepts
query execution and emits timing telemetry to NEXUS without changing behavior.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_SQL_TABLE_RE = re.compile(
    r'(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+["\' `]?(\w+)["\' `]?', re.IGNORECASE
)


def _extract_tables(sql: str) -> list[str]:
    return list(set(m.group(1).lower() for m in _SQL_TABLE_RE.finditer(sql)))


def _sql_from_args(args: tuple[Any, ...]) -> str:
    """Extract SQL string from the first positional argument."""
    if not args:
        return ""
    first = args[0]
    if isinstance(first, str):
        return first[:200]
    if hasattr(first, "text"):
        return str(first.text)[:200]
    if hasattr(first, "string"):
        return str(first.string)[:200]
    return str(first)[:200]


def _emit_query_sync(
    sql: str,
    duration_ms: float,
    track_tables: bool,
    slow_ms: int,
    token: str,
    url: str,
    label: str,
) -> None:
    try:
        import requests

        requests.post(
            f"{url}/sdk/query",
            json={
                "sql_preview": sql or label,
                "duration_ms": round(duration_ms, 1),
                "tables": _extract_tables(sql) if track_tables and sql else [],
                "slow": duration_ms > slow_ms,
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=2,
        )
    except Exception:
        pass


async def _emit_query_async(
    sql: str,
    duration_ms: float,
    track_tables: bool,
    slow_ms: int,
    token: str,
    url: str,
    label: str,
) -> None:
    try:
        import httpx

        async with httpx.AsyncClient(timeout=2.0) as client:
            await client.post(
                f"{url}/sdk/query",
                json={
                    "sql_preview": sql or label,
                    "duration_ms": round(duration_ms, 1),
                    "tables": _extract_tables(sql) if track_tables and sql else [],
                    "slow": duration_ms > slow_ms,
                },
                headers={"Authorization": f"Bearer {token}"},
            )
    except Exception:
        pass


class _DBWrapper:
    """
    Transparent proxy around any DB object.
    Intercepts execution methods and emits timing telemetry to NEXUS.
    """

    _INTERCEPT = frozenset(
        {
            "execute",
            "query",
            "fetch",
            "fetchrow",
            "fetchval",
            "scalar",
            "scalar_one",
            "scalar_one_or_none",
        }
    )

    def __init__(self, wrapped, token, nexus_url, slow_ms, track_tables, label):
        object.__setattr__(self, "_wrapped", wrapped)
        object.__setattr__(self, "_token", token)
        object.__setattr__(self, "_nexus_url", nexus_url)
        object.__setattr__(self, "_slow_ms", slow_ms)
        object.__setattr__(self, "_track_tables", track_tables)
        object.__setattr__(self, "_label", label)

    def __getattr__(self, name: str):
        wrapped = object.__getattribute__(self, "_wrapped")
        attr = getattr(wrapped, name)

        if name not in _DBWrapper._INTERCEPT or not callable(attr):
            return attr

        slow_ms = object.__getattribute__(self, "_slow_ms")
        track_tables = object.__getattribute__(self, "_track_tables")
        token = object.__getattribute__(self, "_token")
        nexus_url = object.__getattribute__(self, "_nexus_url")
        label = object.__getattribute__(self, "_label")

        if inspect.iscoroutinefunction(attr):
            async def async_wrapper(*args, **kwargs):
                sql = _sql_from_args(args)
                start = time.monotonic()
                try:
                    return await attr(*args, **kwargs)
                finally:
                    duration_ms = (time.monotonic() - start) * 1000
                    asyncio.create_task(
                        _emit_query_async(
                            sql,
                            duration_ms,
                            track_tables,
                            slow_ms,
                            token,
                            nexus_url,
                            label,
                        )
                    )

            return async_wrapper
        else:
            def sync_wrapper(*args, **kwargs):
                sql = _sql_from_args(args)
                start = time.monotonic()
                try:
                    return attr(*args, **kwargs)
                finally:
                    duration_ms = (time.monotonic() - start) * 1000
                    threading.Thread(
                        target=_emit_query_sync,
                        args=(
                            sql,
                            duration_ms,
                            track_tables,
                            slow_ms,
                            token,
                            nexus_url,
                            label,
                        ),
                        daemon=True,
                    ).start()

            return sync_wrapper

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_wrapped"), name, value)

    def __enter__(self):
        return object.__getattribute__(self, "_wrapped").__enter__()

    def __exit__(self, *args):
        return object.__getattribute__(self, "_wrapped").__exit__(*args)

    async def __aenter__(self):
        return await object.__getattribute__(self, "_wrapped").__aenter__()

    async def __aexit__(self, *args):
        return await object.__getattribute__(self, "_wrapped").__aexit__(*args)
