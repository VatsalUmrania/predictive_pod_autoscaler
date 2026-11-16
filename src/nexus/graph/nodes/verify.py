"""
NEXUS Post-Execution Verification Node
======================================
Verifies target SLO and health following remediation execution:
  - healthy   → record_post_check_success + cooldown
  - unhealthy → record_post_check_failure (retry or trigger rollback)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from nexus.engine.fsm import IncidentState
from nexus.graph.nodes.verify_helpers import (
    OPERATOR_MAP,
    evaluate_verification_condition,
    extract_metric_value,
    verify_step_conditions,
)
from nexus.graph.platform import TargetResource, get_platform_registry
from nexus.graph.state import IncidentGraphState, VerificationCondition
from nexus.tools.registry import default_registry

logger = logging.getLogger(__name__)

VERIFY_TIMEOUT_S = float(os.getenv("NEXUS_VERIFY_TIMEOUT_S", "120"))
VERIFY_POLL_INTERVAL_S = float(os.getenv("NEXUS_VERIFY_POLL_INTERVAL_S", "5"))


def _get_ladder() -> Any:
    try:
        from nexus.observability.status_api import context

        return context.action_ladder
    except Exception:
        return None


async def verify_node(state: IncidentGraphState) -> dict[str, Any]:
    target_dict = dict(state.get("target") or {})
    platform_id = state.get("platform", "kubernetes")
    if "platform" not in target_dict:
        target_dict["platform"] = platform_id
    target = (
        TargetResource(**target_dict)
        if isinstance(target_dict, dict) and "name" in target_dict
        else TargetResource(
            platform=platform_id, namespace="default", name="unknown", kind="unknown"
        )
    )
    plan = state.get("plan") or state.get("remediation_plan") or {}
    reg = get_platform_registry()
    adapter = reg.get(platform_id) or reg.resolve_adapter(state.get("events", []))

    healthy, slo_restored, details, failure_reason, attempts = False, False, "", "", 0
    deadline = time.monotonic() + VERIFY_TIMEOUT_S

    while True:
        attempts += 1
        verif_result = None
        try:
            verif_result = await adapter.verify_health(target, plan)
            healthy, slo_restored = verif_result.healthy, verif_result.slo_restored
            details = verif_result.details or ""
            failure_reason = verif_result.failure_reason or ""
        except Exception as exc:
            healthy, slo_restored, details, failure_reason = False, False, f"Exception: {exc}", str(exc)

        if healthy and slo_restored:
            steps = plan.get("steps") or []
            conds_ok, cond_details, c_fail = await verify_step_conditions(
                steps, default_registry, verif_result, state.get("telemetry")
            )
            if conds_ok:
                details = f"{details} | {'; '.join(cond_details)}" if details and cond_details else ("; ".join(cond_details) or details)
            else:
                healthy, slo_restored, details, failure_reason = False, False, c_fail, c_fail

        if (healthy and slo_restored) or time.monotonic() >= deadline:
            break
        await asyncio.sleep(VERIFY_POLL_INTERVAL_S)

    retry_count, max_retries = state.get("retry_count", 0), state.get("max_retries", 2)
    now_iso = datetime.now(timezone.utc).isoformat()
    ladder = _get_ladder()
    t_str = f"{getattr(target, 'namespace', 'default')}/{getattr(target, 'name', 'unknown')}"
    records = state.get("execution_records") or []
    action_id = str(records[-1].get("tool_name")) if records else "unknown"

    if healthy and slo_restored:
        if ladder is not None:
            try:
                ladder.record_post_check_success()
                await ladder.set_cooldown(action_id, t_str)
            except Exception as cb_exc:
                logger.warning("[Verify] Ladder update failed: %s", cb_exc)
        return {
            "verification": {
                "healthy": True,
                "slo_restored": True,
                "details": details,
                "attempts": attempts,
            },
            "resolved": True,
            "verification_passed": True,
            "fsm_state": IncidentState.RESOLVED.value,
            "audit_log": [
                {
                    "timestamp": now_iso,
                    "stage": "verify",
                    "action": "slo_restored",
                    "details": details,
                }
            ],
        }

    if ladder is not None:
        try:
            ladder.record_post_check_failure()
        except Exception:
            pass

    if retry_count < max_retries:
        return {
            "verification": {
                "healthy": False,
                "slo_restored": False,
                "details": details,
                "failure_reason": failure_reason,
                "attempts": attempts,
            },
            "verification_passed": False,
            "retry_count": retry_count + 1,
            "fsm_state": IncidentState.RETRYING.value,
            "approval_decision": None,
            "audit_log": [
                {
                    "timestamp": now_iso,
                    "stage": "verify",
                    "action": "retry_scheduled",
                    "details": failure_reason,
                }
            ],
        }

    return {
        "verification": {
            "healthy": False,
            "slo_restored": False,
            "details": details,
            "failure_reason": failure_reason,
            "attempts": attempts,
        },
        "verification_passed": False,
        "fsm_state": IncidentState.ROLLING_BACK.value,
        "audit_log": [
            {
                "timestamp": now_iso,
                "stage": "verify",
                "action": "max_retries_exceeded",
                "details": "Initiating rollback",
            }
        ],
    }
