"""
NEXUS Post-Execution Verification Node
======================================
Verifies that target SLO and health are restored following remediation execution.
Polls the platform adapter's verify_health() contract until healthy or the window
expires (rollout restarts take ~30s+), and updates the Governance Circuit Breaker:
  - healthy      → record_post_check_success + set cooldown for the action/target
  - unhealthy    → record_post_check_failure (may trip the breaker after N failures)
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

# Poll window: real remediations (rollout restarts, config changes) take tens of
# seconds to stabilize. Override via env for tests/tuning.
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
    condition: VerificationCondition | dict[str, Any],
    observed_value: Any,
) -> tuple[bool, str]:
    """
    Safely evaluate a verification condition against an observed value without eval().
    Returns (passed, details).
    """
    if isinstance(condition, dict):
        cond = VerificationCondition(**condition)
    else:
        cond = condition

    op_symbol = cond.operator.strip()
    if op_symbol not in OPERATOR_MAP:
        return False, f"Unsupported or dangerous comparison operator: '{op_symbol}'"

    op_func = OPERATOR_MAP[op_symbol]
    try:
        obs_float = float(observed_value)
        target_float = float(cond.threshold)
        passed = bool(op_func(obs_float, target_float))
        details = (
            f"Metric '{cond.metric_name}' observed={obs_float} "
            f"condition=({op_symbol} {target_float}) -> {'PASS' if passed else 'FAIL'}"
        )
        return passed, details
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
    """Extract numeric metric from tool output, adapter HealthCheckResult, or telemetry."""
    # 1. Check tool_data dictionary
    if isinstance(tool_data, dict):
        if metric_name in tool_data:
            try:
                return float(tool_data[metric_name])
            except (ValueError, TypeError):
                pass
        if (
            "metrics" in tool_data
            and isinstance(tool_data["metrics"], dict)
            and metric_name in tool_data["metrics"]
        ):
            try:
                return float(tool_data["metrics"][metric_name])
            except (ValueError, TypeError):
                pass
        # Check CloudWatch datapoints
        if (
            "datapoints" in tool_data
            and isinstance(tool_data["datapoints"], list)
            and tool_data["datapoints"]
        ):
            dp = tool_data["datapoints"][-1]
            if isinstance(dp, dict):
                for k in ("Average", "Sum", "Maximum", "Minimum", "Value"):
                    if k in dp:
                        try:
                            return float(dp[k])
                        except (ValueError, TypeError):
                            pass
        # If describe output in status text
        if "status" in tool_data and isinstance(tool_data["status"], str):
            status_text = tool_data["status"]
            if metric_name == "ready_replicas":
                m = re.search(r"(\d+)\s+ready", status_text, re.IGNORECASE)
                if m:
                    return float(m.group(1))

    # 2. Check health_result.metrics
    if (
        health_result
        and hasattr(health_result, "metrics")
        and isinstance(health_result.metrics, dict)
    ):
        if metric_name in health_result.metrics:
            try:
                return float(health_result.metrics[metric_name])
            except (ValueError, TypeError):
                pass

    # 3. Check telemetry.metrics
    if isinstance(telemetry, dict):
        metrics = telemetry.get("metrics")
        if isinstance(metrics, dict) and metric_name in metrics:
            try:
                return float(metrics[metric_name])
            except (ValueError, TypeError):
                pass

    return None


def _get_ladder() -> Any:
    """Fetch the ActionLadder from the runtime context (None outside the server)."""
    try:
        from nexus.observability.status_api import context

        return context.action_ladder
    except Exception:
        return None


def _remediation_action_id(state: IncidentGraphState, plan: dict[str, Any]) -> str:
    """Identity of the mutation just executed (last executed step, else first planned)."""
    records = state.get("execution_records") or []
    if records and isinstance(records[-1], dict) and records[-1].get("tool_name"):
        return str(records[-1]["tool_name"])
    steps = plan.get("steps") or []
    if steps and isinstance(steps[0], dict) and steps[0].get("tool_name"):
        return str(steps[0]["tool_name"])
    return "unknown"


async def verify_node(state: IncidentGraphState) -> dict[str, Any]:
    """Poll target health/SLO via the platform adapter until healthy or window expires."""
    incident_id = state["incident_id"]
    target_dict = dict(state.get("target") or {})
    platform_id = state.get("platform", "kubernetes")
    if "platform" not in target_dict:
        target_dict["platform"] = platform_id
    target = (
        TargetResource(**target_dict) if isinstance(target_dict, dict) else target_dict
    )
    plan = state.get("plan") or {}

    registry = get_platform_registry()
    adapter = registry.get(platform_id) or registry.resolve_adapter(
        state.get("events", [])
    )

    healthy = False
    slo_restored = False
    details = ""
    failure_reason = ""
    attempts = 0
    deadline = time.monotonic() + VERIFY_TIMEOUT_S

    # Poll loop: first check immediately (fast path), then wait for stabilization.
    while True:
        attempts += 1
        adapter_healthy = False
        adapter_slo = False
        verif_result = None

        try:
            verif_result = await adapter.verify_health(target, plan)
            adapter_healthy = verif_result.healthy
            adapter_slo = verif_result.slo_restored
            details = verif_result.details or ""
            failure_reason = verif_result.failure_reason or ""
        except Exception as exc:
            logger.warning("[Verify] Verification error for %s: %s", target, exc)
            adapter_healthy = False
            adapter_slo = False
            details = f"Verification exception: {exc}"
            failure_reason = str(exc)

        if not adapter_healthy or not adapter_slo:
            healthy = False
            slo_restored = False
        else:
            # Adapter reported healthy. Check explicit VerificationConditions on plan steps
            conditions_passed = True
            cond_details_list: list[str] = []
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

                v_tool_name = step.get("verification_tool")
                v_tool_args = step.get("verification_parameters") or {}
                tool_data = None

                if v_tool_name and default_registry.get(v_tool_name):
                    try:
                        tool_res = await default_registry.execute_tool(
                            v_tool_name, v_tool_args
                        )
                        if tool_res.success:
                            tool_data = tool_res.data
                    except Exception as t_exc:
                        logger.warning(
                            "[Verify] Verification tool %s error: %s",
                            v_tool_name,
                            t_exc,
                        )

                observed_val = extract_metric_value(
                    metric_name=cond.metric_name,
                    tool_data=tool_data,
                    health_result=verif_result,
                    telemetry=state.get("telemetry"),
                )

                if observed_val is None:
                    logger.info(
                        "[Verify] Metric '%s' unobserved directly; relying on adapter health verdict",
                        cond.metric_name,
                    )
                    cond_details_list.append(
                        f"Metric '{cond.metric_name}' inferred healthy from adapter"
                    )
                    continue

                passed, cond_msg = evaluate_verification_condition(cond, observed_val)
                cond_details_list.append(cond_msg)
                if not passed:
                    conditions_passed = False
                    failure_reason = cond_msg
                    break

            if conditions_passed:
                healthy = True
                slo_restored = True
                if cond_details_list:
                    details = (
                        f"{details} | {'; '.join(cond_details_list)}"
                        if details
                        else "; ".join(cond_details_list)
                    )
            else:
                healthy = False
                slo_restored = False
                details = failure_reason

        if healthy and slo_restored:
            break
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(VERIFY_POLL_INTERVAL_S)

    retry_count = state.get("retry_count", 0)
    max_retries = state.get("max_retries", 2)
    now_iso = datetime.now(timezone.utc).isoformat()

    action_id = _remediation_action_id(state, plan)
    target_str = f"{target.namespace}/{target.name}"
    ladder = _get_ladder()

    if healthy and slo_restored:
        logger.info(
            "[Verify] Incident %s VERIFIED: target %s healthy after %d check(s)",
            incident_id,
            target,
            attempts,
        )

        if ladder is not None:
            try:
                ladder.record_post_check_success()
                await ladder.set_cooldown(action_id, target_str)
            except Exception as cb_exc:
                logger.warning(
                    "[Verify] Circuit-breaker/cooldown update failed: %s", cb_exc
                )

        audit_entry = {
            "timestamp": now_iso,
            "stage": "verify",
            "action": "slo_restored",
            "details": f"Target {target} verified healthy after {attempts} check(s): {details}",
        }
        return {
            "verification": {
                "healthy": True,
                "slo_restored": True,
                "details": details,
                "attempts": attempts,
            },
            "resolved": True,
            "fsm_state": IncidentState.RESOLVED.value,
            "audit_log": [audit_entry],
        }

    # Verification failed — a failed post-check counts toward the governance circuit breaker
    if ladder is not None:
        try:
            ladder.record_post_check_failure()
        except Exception as cb_exc:
            logger.warning("[Verify] Circuit-breaker failure record failed: %s", cb_exc)

    logger.warning(
        "[Verify] Incident %s verification FAILED for %s after %d check(s) (reason: %s). Attempt %d/%d",
        incident_id,
        target,
        attempts,
        failure_reason,
        retry_count + 1,
        max_retries,
    )

    if retry_count < max_retries:
        next_fsm = IncidentState.RETRYING.value
        audit_entry = {
            "timestamp": now_iso,
            "stage": "verify",
            "action": "retry_scheduled",
            "details": f"Health check failed after {attempts} check(s) ({failure_reason}) — scheduling retry ({retry_count + 1}/{max_retries})",
        }
        return {
            "verification": {
                "healthy": False,
                "slo_restored": False,
                "details": details,
                "failure_reason": failure_reason,
                "attempts": attempts,
            },
            "retry_count": retry_count + 1,
            "fsm_state": next_fsm,
            "audit_log": [audit_entry],
        }

    # Max retries exceeded -> Trigger Rollback
    next_fsm = IncidentState.ROLLING_BACK.value
    audit_entry = {
        "timestamp": now_iso,
        "stage": "verify",
        "action": "max_retries_exceeded",
        "details": f"Health check failed after {attempts} check(s) over {VERIFY_TIMEOUT_S}s — initiating deterministic rollback",
    }

    return {
        "verification": {
            "healthy": False,
            "slo_restored": False,
            "details": details,
            "failure_reason": failure_reason,
            "attempts": attempts,
        },
        "fsm_state": next_fsm,
        "audit_log": [audit_entry],
    }
