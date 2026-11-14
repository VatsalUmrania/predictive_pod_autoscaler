"""
NEXUS Governance & Safety Gate Node
===================================
Enforces deterministic symbolic safety policies before ANY action is executed:
  1. OPA Policy Engine rules
  2. Universal Human Approval requirement
  3. Live State Validator (checks if target is actually broken or already self-healed)
  4. Cooldown Store (prevents thrashing/rapid re-execution)
  5. Governance Circuit Breaker (suspends autonomous healing if tripped)
"""

from __future__ import annotations

import logging
import os
import zlib
from datetime import datetime, timezone
from typing import Any

from nexus.db import get_database_client
from nexus.engine.fsm import IncidentState
from nexus.governance.cooldown_store import CooldownStore
from nexus.governance.live_state_validator import check_live_state
from nexus.governance.policy_engine import PolicyEngine
from nexus.graph.platform import TargetResource
from nexus.graph.state import GovernanceVerdict, IncidentGraphState
from nexus.tools.registry import default_registry

logger = logging.getLogger(__name__)


def _get_ladder() -> Any:
    """Fetch the ActionLadder from the runtime context (None outside the server)."""
    try:
        from nexus.observability.status_api import context

        return context.action_ladder
    except Exception:
        return None


def _get_policy_engine() -> PolicyEngine | None:
    """Fetch PolicyEngine from runtime context or environment configuration."""
    try:
        from nexus.observability.status_api import context

        if hasattr(context, "policy_engine") and context.policy_engine is not None:
            return context.policy_engine
    except Exception:
        pass
    if os.getenv("NEXUS_OPA_ENABLED", "").lower() in ("1", "true", "yes") or os.getenv(
        "NEXUS_OPA_URL"
    ):
        opa_url = os.getenv("NEXUS_OPA_URL", "http://localhost:8181")
        return PolicyEngine(opa_url=opa_url)
    return None


async def govern_node(state: IncidentGraphState) -> dict[str, Any]:
    """Evaluate symbolic safety, policy, and live state before execution."""
    incident_id = state["incident_id"]
    plan_obj = state.get("plan")
    if isinstance(plan_obj, dict):
        plan = plan_obj
    elif hasattr(plan_obj, "model_dump"):
        plan = plan_obj.model_dump()
    elif hasattr(plan_obj, "dict"):
        plan = plan_obj.dict()
    else:
        plan = {}

    steps = plan.get("steps", [])
    target_dict = dict(state.get("target") or {})
    platform_id = state.get("platform", "kubernetes")
    if "platform" not in target_dict:
        target_dict["platform"] = platform_id
    target = (
        TargetResource(**target_dict) if isinstance(target_dict, dict) else target_dict
    )

    target_service = (
        target.name
        if hasattr(target, "name")
        else str(target_dict.get("name", "unknown"))
    )
    anomaly_signal = state.get("anomaly_signal")
    if isinstance(anomaly_signal, dict):
        environment = anomaly_signal.get("environment")
    elif anomaly_signal is not None:
        environment = getattr(anomaly_signal, "environment", None)
    else:
        environment = None
    if not environment:
        environment = os.getenv("NEXUS_ENVIRONMENT", "production")

    diag = state.get("diagnosis")
    if isinstance(diag, dict):
        confidence = float(diag.get("confidence", 0.85))
    elif diag is not None:
        confidence = float(getattr(diag, "confidence", 0.85))
    else:
        confidence = 0.85

    prior_decision = str(state.get("approval_decision", "")).lower()
    operator_confirmed = prior_decision in (
        "approved",
        "yes",
        "proceed",
        "edit",
        "modify",
    )

    allowed = True
    requires_approval = plan.get("requires_approval", False)
    reasons: list[str] = []
    if plan.get("approval_reason"):
        reasons.append(plan["approval_reason"])

    is_self_healed = False
    cb_open = False
    cooldown_active = False

    # 1. PostgreSQL Advisory Locking (prevent concurrent mutations on same service)
    conn = None
    conn_ctx = None
    lock_acquired = False
    lock_key = zlib.crc32(target_service.encode())

    try:
        db_client = await get_database_client()
        conn_ctx = db_client.acquire()
        conn = await conn_ctx.__aenter__()
        if hasattr(conn, "fetchval"):
            lock_res = await conn.fetchval("SELECT pg_try_advisory_lock($1)", lock_key)
            lock_acquired = bool(lock_res)
            if not lock_acquired:
                logger.warning(
                    "[Govern] Could not acquire advisory lock for service %s (key=%d); concurrent action in progress",
                    target_service,
                    lock_key,
                )
                allowed = False
                reasons.append(
                    f"Concurrent mutation locked on service '{target_service}' (advisory lock held)"
                )
    except Exception as db_exc:
        logger.debug(
            "[Govern] PostgreSQL advisory lock skipped/unavailable: %s", db_exc
        )

    try:
        # 2. Live State Validation (Kill-switch if target already self-healed)
        if platform_id == "kubernetes" and allowed:
            try:
                first_tool = steps[0].get("tool_name", "") if steps else ""
                action_type = "restart_pod"
                if "scale" in first_tool:
                    action_type = "scale"
                elif "rollback" in first_tool:
                    action_type = "rollback_deployment"

                live_report = await check_live_state(
                    action_type=action_type,
                    namespace=target.namespace,
                    resource_name=target.name,
                    k8s_core=None,
                    k8s_apps=None,
                )

                if live_report.verdict == "self_healed":
                    logger.info(
                        "[Govern] Target %s already self-healed — cancelling mutation and resolving incident",
                        target,
                    )
                    allowed = False
                    is_self_healed = True
                    reasons.append(
                        f"Target already self-healed: {live_report.evidence}"
                    )
            except Exception as lsv_err:
                logger.debug(
                    "[Govern] check_live_state check skipped/errored: %s", lsv_err
                )

        # 3. Governance Circuit Breaker + Cooldown (wired from ActionLadder via NexusContext)
        if allowed and steps and not is_self_healed:
            ladder = _get_ladder()
            if ladder is not None:
                action_id = str(steps[0].get("tool_name", "unknown"))
                target_str = f"{target.namespace}/{target.name}"
                if ladder.governance_cb.is_open:
                    cb_open = True
                    allowed = False
                    reasons.append(
                        "governance circuit breaker open "
                        f"({ladder.governance_cb.consecutive_failures} consecutive post-check failures) "
                        "— autonomous healing suspended"
                    )
                else:
                    try:
                        cooldown_key = CooldownStore.make_key(action_id, target_str)
                        if await ladder.cooldown_store.is_in_cooldown(cooldown_key):
                            cooldown_active = True
                            allowed = False
                            remaining = await ladder.cooldown_store.remaining_seconds(
                                cooldown_key
                            )
                            reasons.append(
                                f"target in cooldown for {action_id} ({remaining:.0f}s remaining) "
                                "— rapid re-execution blocked"
                            )
                    except Exception as cd_err:
                        logger.warning(
                            "[Govern] Cooldown check failed (fail-open): %s", cd_err
                        )

        # 4. Zero-Trust Metadata Enrichment & OPA Policy Evaluation
        if allowed and steps and not is_self_healed:
            policy_engine = _get_policy_engine()

            for raw_step in steps:
                step = (
                    raw_step
                    if isinstance(raw_step, dict)
                    else (
                        raw_step.model_dump()
                        if hasattr(raw_step, "model_dump")
                        else raw_step.dict()
                        if hasattr(raw_step, "dict")
                        else vars(raw_step)
                    )
                )
                step_tool_name = str(step.get("tool_name", ""))
                tool_meta = default_registry.get(step_tool_name)

                if tool_meta is not None:
                    is_destructive = tool_meta.is_destructive
                    blast_radius = tool_meta.blast_radius
                    allows_unbacked_rollback = tool_meta.allows_unbacked_rollback
                else:
                    is_destructive = False
                    blast_radius = "single_deployment"
                    allows_unbacked_rollback = False

                has_rollback = bool(step.get("rollback_tool"))

                # In-process Zero-Trust Defense in Depth
                if is_destructive:
                    allowed = False
                    reasons.append(
                        f"Tool '{step_tool_name}' is destructive — blocked by zero-trust governance"
                    )
                if blast_radius == "account_wide" and not step.get(
                    "override_blast_radius"
                ):
                    allowed = False
                    reasons.append(
                        f"Tool '{step_tool_name}' has account_wide blast radius — blocked by zero-trust governance"
                    )
                if environment == "production" and blast_radius == "service_wide":
                    requires_approval = True
                if not allows_unbacked_rollback and not has_rollback:
                    requires_approval = True
                    if not operator_confirmed:
                        reasons.append(
                            f"Tool '{step_tool_name}' missing rollback step — requires human approval"
                        )

                # Remote OPA Engine Evaluation (if configured)
                if policy_engine is not None and allowed:
                    decision = await policy_engine.evaluate(
                        tool_name=step_tool_name,
                        action_type=step_tool_name,
                        parameters=step.get("parameters", {}),
                        blast_radius=blast_radius,
                        is_destructive=is_destructive,
                        allows_unbacked_rollback=allows_unbacked_rollback,
                        has_rollback=has_rollback,
                        in_cooldown=cooldown_active,
                        governance_cb_open=cb_open,
                        confidence=confidence,
                        human_approved=operator_confirmed,
                        environment=environment,
                        target_service=target_service,
                    )
                    if not decision.allowed:
                        allowed = False
                        reasons.extend(
                            decision.deny_reasons or ["OPA policy denied action"]
                        )
                    if decision.requires_approval:
                        requires_approval = True

        # 5. Universal Human Approval Requirement
        if steps and not is_self_healed:
            if operator_confirmed:
                requires_approval = False
            else:
                requires_approval = True
                if "Human authorization required" not in " ".join(reasons):
                    reasons.append(
                        "Human authorization required for all remediation actions"
                    )
        elif not steps:
            requires_approval = False

        # 6. Formulate Governance Verdict
        verdict = GovernanceVerdict(
            allowed=allowed,
            requires_human_approval=requires_approval,
            circuit_breaker_open=cb_open,
            cooldown_active=cooldown_active,
            live_state_valid=not is_self_healed,
            self_healed=is_self_healed,
            reasons=reasons,
        )

        # Decide next FSM state
        if is_self_healed:
            next_fsm = IncidentState.RESOLVED.value
        elif not allowed:
            next_fsm = IncidentState.FAILED.value
        elif requires_approval:
            next_fsm = IncidentState.APPROVAL_PENDING.value
        else:
            next_fsm = IncidentState.EXECUTING.value

        now_iso = datetime.now(timezone.utc).isoformat()
        audit_entry = {
            "timestamp": now_iso,
            "stage": "govern",
            "action": "policy_evaluated",
            "details": f"Allowed: {allowed} | Self-Healed: {is_self_healed} | Approval Required: {requires_approval} | Reasons: {'; '.join(reasons) or 'None'}",
        }

        logger.info(
            "[Govern] Incident %s: allowed=%s, self_healed=%s, requires_approval=%s, next_fsm=%s",
            incident_id,
            allowed,
            is_self_healed,
            requires_approval,
            next_fsm,
        )

        result_payload: dict[str, Any] = {
            "governance": verdict.model_dump(),
            "fsm_state": next_fsm,
            "audit_log": [audit_entry],
        }
        if is_self_healed:
            result_payload["resolved"] = True

        return result_payload

    finally:
        # Release short-lived advisory lock before transitioning to next node (e.g. approval)
        if conn is not None and lock_acquired:
            try:
                await conn.execute("SELECT pg_advisory_unlock($1)", lock_key)
                logger.debug("[Govern] Released advisory lock for key %d", lock_key)
            except Exception as unlock_err:
                logger.warning(
                    "[Govern] Failed to release advisory lock: %s", unlock_err
                )
        if conn_ctx is not None:
            try:
                await conn_ctx.__aexit__(None, None, None)
            except Exception:
                pass
