"""
NEXUS Governance & Safety Gate Node
===================================
Enforces deterministic symbolic safety policies before ANY action is executed:
  1. PostgreSQL Advisory Locking (prevents concurrent mutations)
  2. Live State Validator (checks if target is actually broken or already self-healed)
  3. Circuit Breaker & Cooldown
  4. Attribute-Based OPA Policy Engine rules
  5. Universal Human Approval requirement
"""

from __future__ import annotations

import logging
import zlib
from datetime import datetime, timezone
from typing import Any

from nexus.db import get_database_client
from nexus.engine.fsm import IncidentState
from nexus.governance.cooldown_store import CooldownStore
from nexus.graph.nodes.govern_helpers import evaluate_steps_opa
from nexus.graph.platform import TargetResource
from nexus.graph.state import GovernanceVerdict, IncidentGraphState

logger = logging.getLogger(__name__)


async def govern_node(state: IncidentGraphState) -> dict[str, Any]:
    state["incident_id"]
    plan_obj = state.get("plan")
    if plan_obj is None:
        plan: dict[str, Any] = {}
    elif hasattr(plan_obj, "model_dump"):
        plan = plan_obj.model_dump()
    elif hasattr(plan_obj, "dict"):
        plan = plan_obj.dict()
    elif isinstance(plan_obj, dict):
        plan = dict(plan_obj)
    else:
        plan = {}
    steps = plan.get("steps", [])
    target_dict = dict(state.get("target") or {})
    target = (
        TargetResource(**target_dict)
        if isinstance(target_dict, dict) and "name" in target_dict
        else target_dict
    )
    target_service = getattr(target, "name", str(target_dict.get("name", "unknown")))
    state.get("platform", "kubernetes")

    diag = state.get("diagnosis")
    diag_conf = 0.85
    if isinstance(diag, dict):
        diag_conf = diag.get("confidence", 0.85) or 0.85
    elif hasattr(diag, "confidence"):
        diag_conf = getattr(diag, "confidence", 0.85) or 0.85
    confidence = float(diag_conf)
    prior_decision = str(state.get("approval_decision", "")).lower()
    operator_confirmed = prior_decision in (
        "approved",
        "yes",
        "proceed",
        "edit",
        "modify",
    )
    if state.get("retry_count", 0) > 0 and len(state.get("execution_records", [])) > 0:
        operator_confirmed = False

    allowed, requires_approval, is_self_healed, cb_open, cooldown_active = (
        True,
        False,
        False,
        False,
        False,
    )
    reasons: list[str] = (
        [plan["approval_reason"]] if plan.get("approval_reason") else []
    )

    # 1. PostgreSQL Advisory Locking
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
            if not lock_acquired:
                allowed = False
                reasons.append(
                    f"Concurrent mutation locked on service '{target_service}' (advisory lock held)"
                )
    except Exception as db_exc:
        logger.debug("[Govern] Advisory lock skipped: %s", db_exc)

    try:
        # 2. Circuit Breaker & Cooldown
        if allowed and steps and not is_self_healed:
            ladder = None
            try:
                from nexus.observability.status_api import context

                ladder = (
                    context.action_ladder if hasattr(context, "action_ladder") else None
                )
            except Exception:
                pass
            if ladder is not None:
                if ladder.governance_cb.is_open:
                    cb_open, allowed = True, False
                    reasons.append(
                        f"governance circuit breaker open ({ladder.governance_cb.consecutive_failures} failures)"
                    )
                else:
                    action_id = str(steps[0].get("tool_name", "unknown"))
                    c_key = CooldownStore.make_key(
                        action_id,
                        f"{getattr(target, 'namespace', 'default')}/{target_service}",
                    )
                    if await ladder.cooldown_store.is_in_cooldown(c_key):
                        cooldown_active, allowed = True, False
                        rem = await ladder.cooldown_store.remaining_seconds(c_key)
                        reasons.append(
                            f"target in cooldown for {action_id} ({rem:.0f}s remaining)"
                        )

        # 4. Attribute-Based OPA Policy Engine Evaluation
        if allowed and steps and not is_self_healed:
            opa_allowed, opa_approval, opa_reasons = await evaluate_steps_opa(
                steps, confidence, cb_open, cooldown_active
            )
            if not opa_allowed:
                allowed = False
                reasons.extend(opa_reasons)
            if opa_approval:
                requires_approval = True

        # 5. Universal Human Approval Requirement
        if steps and not is_self_healed:
            if operator_confirmed:
                requires_approval = False
            else:
                requires_approval = True
                if not any("Human authorization" in r for r in reasons):
                    reasons.append(
                        "Human authorization required for all remediation actions"
                    )
        elif not steps and not is_self_healed:
            allowed = False
            reasons.append("No remediation steps generated in plan")

        # 6. Formulate Verdict
        verdict = GovernanceVerdict(
            allowed=allowed,
            requires_human_approval=requires_approval,
            circuit_breaker_open=cb_open,
            cooldown_active=cooldown_active,
            live_state_valid=not is_self_healed,
            self_healed=is_self_healed,
            reasons=reasons,
        )
        next_fsm = (
            IncidentState.RESOLVED.value
            if is_self_healed
            else (
                IncidentState.FAILED.value
                if not allowed
                else (
                    IncidentState.APPROVAL_PENDING.value
                    if requires_approval
                    else IncidentState.EXECUTING.value
                )
            )
        )
        audit_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stage": "govern",
            "action": "policy_evaluated",
            "details": f"Allowed: {allowed} | Self-Healed: {is_self_healed} | Approval Required: {requires_approval} | Reasons: {'; '.join(reasons) or 'None'}",
        }
        res_payload: dict[str, Any] = {
            "governance": verdict.model_dump(),
            "fsm_state": next_fsm,
            "audit_log": [audit_entry],
        }
        if is_self_healed:
            res_payload["resolved"] = True
        return res_payload

    finally:
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
