"""
Helpers for NEXUS Governance Node.
"""
from __future__ import annotations

import inspect
import logging
import os
import zlib
from typing import Any, cast

from nexus.db import get_database_client
from nexus.governance.cooldown_store import CooldownStore
from nexus.governance.live_state_validator import check_live_state
from nexus.governance.policy_engine import PolicyEngine, build_opa_input_for_action

logger = logging.getLogger(__name__)


def get_policy_engine() -> PolicyEngine:
    try:
        from nexus.observability.status_api import context

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
            lock_acquired = bool(await conn.fetchval("SELECT pg_try_advisory_lock($1)", lock_key))
    except Exception as db_exc:
        logger.debug("[Govern] Advisory lock skipped: %s", db_exc)
    return conn, conn_ctx, lock_key, lock_acquired


async def release_advisory_lock(conn: Any, conn_ctx: Any, lock_key: int, lock_acquired: bool) -> None:
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


async def check_live_validation(platform_id: str, target: Any, target_service: str, steps: list) -> tuple[bool, str]:
    if platform_id != "kubernetes" or not steps:
        return False, ""
    try:
        first_tool = steps[0].get("tool_name", "") if steps else ""
        action_type = "scale" if "scale" in first_tool else ("rollback_deployment" if "rollback" in first_tool else "restart_pod")
        live_report = await check_live_state(
            action_type=action_type,
            namespace=getattr(target, "namespace", "default"),
            resource_name=target_service,
            k8s_core=None,
            k8s_apps=None,
        )
        if live_report.verdict == "self_healed":
            return True, live_report.evidence
    except Exception as lsv_err:
        logger.debug("[Govern] Live state validation skipped: %s", lsv_err)
    return False, ""


async def evaluate_steps_opa(steps: list, confidence: float, cb_open: bool, cooldown_active: bool) -> tuple[bool, bool, list[str]]:
    engine = get_policy_engine()
    allowed, requires_approval, reasons = True, False, []
    for raw_step in steps:
        step = raw_step if isinstance(raw_step, dict) else (raw_step.model_dump() if hasattr(raw_step, "model_dump") else vars(raw_step))
        inp = build_opa_input_for_action(step, {"confidence": confidence}, cooldown_active=cooldown_active, circuit_breaker_tripped=cb_open)
        call = engine.evaluate_action(inp)
        res = await call if inspect.isawaitable(call) else call
        dec = res.get("decision", "DENY")
        if dec == "DENY":
            allowed = False
            reasons.extend(res.get("deny_reasons") or ["OPA policy denied action"])
        elif dec == "REQUIRE_APPROVAL":
            requires_approval = True
    return allowed, requires_approval, reasons
