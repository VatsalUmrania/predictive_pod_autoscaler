"""
Helper functions for NEXUS verification node.
"""

from __future__ import annotations

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
