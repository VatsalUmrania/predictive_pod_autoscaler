"""
Attribute-Based Governance Helpers for NEXUS.
Relies exclusively on ToolRegistry metadata and OPA policy evaluation
(zero hardcoded Python sets of allowed tool names).
"""

from __future__ import annotations

import logging
import os
import zlib
from typing import Any, cast

from nexus.core.database import get_database_client
from nexus.governance.policies.policy_engine import PolicyEngine, evaluate_opa_policy

logger = logging.getLogger(__name__)


def get_policy_engine() -> PolicyEngine:
    try:
        from nexus.services.api.status.status_api import context

        if hasattr(context, "policy_engine") and context.policy_engine is not None:
            return cast(PolicyEngine, context.policy_engine)
    except Exception:
        pass
    opa_url = os.getenv("NEXUS_OPA_URL", "http://localhost:8181")
    return PolicyEngine(opa_url=opa_url, fallback_to_local=True)


async def acquire_advisory_lock(target_service: str) -> tuple[Any, Any, int, bool]:
    conn, conn_ctx, lock_acquired = None, None, False
    lock_key = zlib.crc32(target_service.encode())
    try:
        db_client = await get_database_client()
        conn_ctx = db_client.acquire()
        conn = await conn_ctx.__aenter__()
        if hasattr(conn, "fetchval"):
            lock_acquired = bool(
                await conn.fetchval("SELECT pg_try_advisory_lock($1)", lock_key)
            )
    except Exception as db_exc:
        logger.debug("[Govern] Advisory lock skipped: %s", db_exc)
    return conn, conn_ctx, lock_key, lock_acquired


async def release_advisory_lock(
    conn: Any, conn_ctx: Any, lock_key: int, lock_acquired: bool
) -> None:
    if conn is not None and lock_acquired:
        try:
            await conn.execute("SELECT pg_advisory_unlock($1)", lock_key)
        except Exception as unlock_err:
            logger.warning("[Govern] Failed to release lock: %s", unlock_err)
    if conn_ctx is not None:
        try:
            await conn_ctx.__aexit__(None, None, None)
        except Exception:
            pass


__all__ = [
    "get_policy_engine",
    "acquire_advisory_lock",
    "release_advisory_lock",
    "evaluate_opa_policy",
]
