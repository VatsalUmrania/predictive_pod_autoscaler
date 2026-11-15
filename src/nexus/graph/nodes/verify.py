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
import operator
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

from nexus.engine.fsm import IncidentState
from nexus.graph.platform import TargetResource, get_platform_registry
from nexus.graph.state import IncidentGraphState, VerificationCondition
from nexus.tools.registry import default_registry

logger = logging.getLogger(__name__)

VERIFY_TIMEOUT_S = float(os.getenv("NEXUS_VERIFY_TIMEOUT_S", "120"))
VERIFY_POLL_INTERVAL_S = float(os.getenv("NEXUS_VERIFY_POLL_INTERVAL_S", "5"))
OPERATOR_MAP = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
}


def evaluate_verification_condition(
    condition: VerificationCondition | dict[str, Any], observed_value: Any
) -> tuple[bool, str]:
    cond = (
        VerificationCondition(**condition) if isinstance(condition, dict) else condition
    )
    op_symbol = cond.operator.strip()
    if op_symbol not in OPERATOR_MAP:
        return False, f"Unsupported or dangerous comparison operator: '{op_symbol}'"
    try:
        passed = bool(
            OPERATOR_MAP[op_symbol](float(observed_value), float(cond.threshold))
        )
        status_str = "PASS" if passed else "FAIL"
        return (
            passed,
            f"Metric '{cond.metric_name}' observed={float(observed_value)} ({op_symbol} {float(cond.threshold)}) -> {status_str}",
        )
    except (ValueError, TypeError) as exc:
        return (
            False,
            f"Could not evaluate metric '{cond.metric_name}': invalid observed value '{observed_value}' ({exc})",
        )


def extract_metric_value(
    metric_name: str,
    tool_data: Any = None,
    health_result: Any = None,
    telemetry: Any = None,
) -> float | None:
    sources = [tool_data, getattr(health_result, "metrics", None)]
    if isinstance(telemetry, dict):
        sources.append(telemetry.get("metrics"))
    for source in sources:
        if not isinstance(source, dict):
            continue
        if metric_name in source:
            try:
                return float(source[metric_name])
            except (ValueError, TypeError):
                pass
        nested = source.get("metrics")
        if isinstance(nested, dict) and metric_name in nested:
            try:
                return float(nested[metric_name])
            except (ValueError, TypeError):
                pass
        dps = source.get("datapoints")
        if isinstance(dps, list) and dps:
            dp = dps[-1]
            if isinstance(dp, dict):
                for k in ("Average", "Sum", "Maximum", "Minimum", "Value"):
                    if k in dp:
                        try:
                            return float(dp[k])
                        except (ValueError, TypeError):
                            pass
        status_val = source.get("status")
        if isinstance(status_val, str) and metric_name == "ready_replicas":
            m = re.search(r"(\d+)\s+ready", status_val, re.IGNORECASE)
            if m:
                return float(m.group(1))
    return None


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
            details, failure_reason = (
                verif_result.details or "",
                verif_result.failure_reason or "",
            )
        except Exception as exc:
            healthy, slo_restored, details, failure_reason = (
                False,
                False,
                f"Exception: {exc}",
                str(exc),
            )

        if healthy and slo_restored:
            conds_passed, cond_details = True, []
            steps = plan.get("steps") or []
            for step in steps:
                cond_dict = step.get("verification_condition")
                if not cond_dict:
                    continue
                cond = (
                    VerificationCondition(**cond_dict)
                    if isinstance(cond_dict, dict)
                    else cond_dict
                )
                t_data = None
                v_tool = step.get("verification_tool")
                if v_tool and default_registry.get(v_tool):
                    try:
                        res = await default_registry.execute_tool(
                            v_tool, step.get("verification_parameters") or {}
                        )
                        if res.success:
                            t_data = res.data
                    except Exception as te:
                        logger.warning("[Verify] Tool %s error: %s", v_tool, te)
                obs = extract_metric_value(
                    cond.metric_name,
                    tool_data=t_data,
                    health_result=verif_result,
                    telemetry=state.get("telemetry"),
                )
                if obs is None:
                    cond_details.append(
                        f"Metric '{cond.metric_name}' inferred healthy from adapter"
                    )
                    continue
                passed, c_msg = evaluate_verification_condition(cond, obs)
                cond_details.append(c_msg)
                if not passed:
                    conds_passed, failure_reason = False, c_msg
                    break
            if conds_passed:
                details = (
                    f"{details} | {'; '.join(cond_details)}"
                    if details and cond_details
                    else ("; ".join(cond_details) or details)
                )
            else:
                healthy, slo_restored, details = False, False, failure_reason

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
