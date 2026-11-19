import logging
import time
from typing import Any

from nexus.engine.tools.registry import get_tool_registry
from nexus.engine.workflow.state import IncidentState

logger = logging.getLogger(__name__)


async def execute_node(state: IncidentState) -> dict[str, Any]:
    incident_id = state.get("incident_id", "unknown")
    plan = state.get("remediation_plan") or {}
    steps: list[dict[str, Any]] = plan.get("steps") or []

    if not steps:
        logger.error("Incident %s entered execute_node with 0 steps.", incident_id)
        return {
            "execution_status": "FAILED",
            "execution_results": [],
            "status": "EXECUTION_FAILED",
            "error": "No executable steps in remediation_plan.",
        }

    registry = get_tool_registry()
    results: list[dict[str, Any]] = list(state.get("execution_results") or [])

    for step in steps:
        step_num = step.get("step", len(results) + 1)
        tool_name = step["tool_name"]
        tool_args = step.get("tool_args") or {}
        tool_fn = registry.get_tool(tool_name)

        if tool_fn is None:
            logger.error(
                "Incident %s tool '%s' not found in ToolRegistry.",
                incident_id,
                tool_name,
            )
            results.append(
                {
                    "step": step_num,
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "status": "FAILED",
                    "output": f"Tool '{tool_name}' not found in registry.",
                }
            )
            return {
                "execution_status": "FAILED",
                "execution_results": results,
                "remediation_attempts": state.get("remediation_attempts", 0) + 1,
                "status": "EXECUTION_FAILED",
            }

        start_ts = time.monotonic()
        try:
            logger.info(
                "Incident %s executing tool %s(%s)", incident_id, tool_name, tool_args
            )
            output = await tool_fn.ainvoke(tool_args)
            duration_ms = round((time.monotonic() - start_ts) * 1000, 2)

            results.append(
                {
                    "step": step_num,
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "status": "SUCCESS",
                    "duration_ms": duration_ms,
                    "output": str(output),
                }
            )
        except Exception as exc:
            duration_ms = round((time.monotonic() - start_ts) * 1000, 2)
            logger.exception(
                "Incident %s tool %s execution failed: %s", incident_id, tool_name, exc
            )
            results.append(
                {
                    "step": step_num,
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "status": "FAILED",
                    "duration_ms": duration_ms,
                    "output": str(exc),
                }
            )
            return {
                "execution_status": "FAILED",
                "execution_results": results,
                "remediation_attempts": state.get("remediation_attempts", 0) + 1,
                "status": "EXECUTION_FAILED",
            }

    return {
        "execution_status": "SUCCESS",
        "execution_results": results,
        "remediation_attempts": state.get("remediation_attempts", 0) + 1,
        "rollback_available": any(s.get("reversible", True) for s in steps),
        "status": "EXECUTED",
    }
