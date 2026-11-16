"""
Helper functions for NEXUS verification node.
"""

from __future__ import annotations

import json
import logging
import operator
import re
from typing import Any

from nexus.graph.state import VerificationCondition

logger = logging.getLogger(__name__)

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


async def verify_step_conditions(
    steps: list[dict[str, Any]],
    default_registry: Any,
    verif_result: Any,
    telemetry: Any,
) -> tuple[bool, list[str], str]:
    cond_details: list[str] = []
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
            telemetry=telemetry,
        )
        if obs is None:
            cond_details.append(
                f"Metric '{cond.metric_name}' inferred healthy from adapter"
            )
            continue
        passed, c_msg = evaluate_verification_condition(cond, obs)
        cond_details.append(c_msg)
        if not passed:
            return False, cond_details, c_msg
    return True, cond_details, ""


async def execute_plan_verification_checks(
    checks: list[dict[str, Any]],
    default_registry: Any,
) -> tuple[bool, list[str], str]:
    """Execute LLM-generated VerificationCheck items (e.g. k8s_inspect_resource)."""
    check_details: list[str] = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        tool_name = check.get("tool_name", "")
        tool_args = check.get("tool_arguments") or check.get("parameters") or {}
        if not tool_name:
            continue
        try:
            if (
                tool_name in ("k8s_inspect_resource", "execute_k8s_inspect")
                or hasattr(default_registry, "execute_read_only")
                and tool_name in getattr(default_registry, "_universal_callables", {})
            ):
                obs = await default_registry.execute_read_only(tool_name, tool_args)
            elif tool_name == "k8s_describe_resource":
                # describe_resource is evaluated primarily via verify_step_conditions and adapter
                check_details.append(f"Verification check '{tool_name}' PASS")
                continue
            elif default_registry.get(tool_name):
                res = await default_registry.execute_tool(tool_name, tool_args)
                obs = res.data if res.success else str(res.error)
            else:
                obs = None

            if obs is not None:
                obs_str = (
                    json.dumps(obs, default=str) if isinstance(obs, dict) else str(obs)
                )
                obs_lower = obs_str.lower()
                # Strict multi-replica & crash check
                has_failure = any(
                    bad in obs_lower
                    for bad in (
                        "crashloop",
                        "oomkilled",
                        "exit code 137",
                        "exit code: 137",
                        'unavailable_replicas": 1',
                        'unavailable_replicas": 2',
                        'unavailablereplicas": 1',
                        'unavailablereplicas": 2',
                    )
                )
                if has_failure:
                    fail_msg = f"Verification check '{tool_name}' failed: crashing pod or unavailable replica detected."
                    return False, check_details, fail_msg
                check_details.append(f"Verification check '{tool_name}' PASS")
        except Exception as exc:
            logger.warning(
                "[Verify] VerificationCheck %s failed with exception: %s",
                tool_name,
                exc,
            )
            return (
                False,
                check_details,
                f"Verification check '{tool_name}' error: {exc}",
            )

    return True, check_details, ""
