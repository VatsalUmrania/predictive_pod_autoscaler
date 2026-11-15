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
import os
import zlib
from datetime import datetime, timezone
from typing import Any, cast

from nexus.db import get_database_client
from nexus.engine.fsm import IncidentState
from nexus.governance.cooldown_store import CooldownStore
from nexus.governance.live_state_validator import check_live_state
from nexus.governance.policy_engine import PolicyEngine, build_opa_input_for_action
from nexus.graph.platform import TargetResource
from nexus.graph.state import GovernanceVerdict, IncidentGraphState

logger = logging.getLogger(__name__)


def _get_policy_engine() -> PolicyEngine:
    try:
        from nexus.observability.status_api import context

        if hasattr(context, "policy_engine") and context.policy_engine is not None:
            return cast(PolicyEngine, context.policy_engine)
    except Exception:
        pass
    opa_url = os.getenv("NEXUS_OPA_URL", "http://localhost:8181")
    return PolicyEngine(opa_url=opa_url, fallback_to_local=True)


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
    platform_id = state.get("platform", "kubernetes")

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
        # 2. Live State Validation
        if platform_id == "kubernetes" and allowed:
            try:
                first_tool = steps[0].get("tool_name", "") if steps else ""
                action_type = (
                    "scale"
                    if "scale" in first_tool
                    else (
                        "rollback_deployment"
                        if "rollback" in first_tool
                        else "restart_pod"
                    )
                )
                live_report = await check_live_state(
                    action_type=action_type,
                    namespace=getattr(target, "namespace", "default"),
                    resource_name=target_service,
                    k8s_core=None,
                    k8s_apps=None,
                )
                if live_report.verdict == "self_healed":
                    allowed, is_self_healed = False, True
                    reasons.append(
                        f"Target already self-healed: {live_report.evidence}"
                    )
            except Exception as lsv_err:
                logger.debug("[Govern] Live state validation skipped: %s", lsv_err)

        # 3. Circuit Breaker & Cooldown
        if allowed and steps and not is_self_healed:
            try:
                from nexus.observability.status_api import context

                ladder = (
                    context.action_ladder if hasattr(context, "action_ladder") else None
                )
            except Exception:
                ladder = None
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
            engine = _get_policy_engine()
            for raw_step in steps:
                step = (
                    raw_step
                    if isinstance(raw_step, dict)
                    else (
                        raw_step.model_dump()
                        if hasattr(raw_step, "model_dump")
                        else vars(raw_step)
                    )
                )
                inp = build_opa_input_for_action(
                    step,
                    {"confidence": confidence},
                    cooldown_active=cooldown_active,
                    circuit_breaker_tripped=cb_open,
                )
                res = await engine.evaluate_action(inp)
                dec = res.get("decision", "DENY")
                if dec == "DENY":
                    allowed = False
                    reasons.extend(
                        res.get("deny_reasons") or ["OPA policy denied action"]
                    )
                elif dec == "REQUIRE_APPROVAL":
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
