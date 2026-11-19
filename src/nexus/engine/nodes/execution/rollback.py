"""
NEXUS Deterministic Rollback Node
=================================
Reverts executed reversible mutations in LIFO order when execution or
verification exhausts its retry budget.
"""

from __future__ import annotations

import logging
from typing import Any

from nexus.engine.tools.registry import get_tool_registry
from nexus.engine.workflow.state.state import IncidentState

logger = logging.getLogger(__name__)


async def rollback_node(state: IncidentState) -> dict[str, Any]:
    """Execute inverse rollback actions for previously executed steps in reverse order."""
    incident_id = state.get("incident_id", "unknown")
    execution_results: list[dict[str, Any]] = list(state.get("execution_results") or [])
    plan_steps = (state.get("remediation_plan") or {}).get("steps") or []
    steps_by_num = {s.get("step", idx): s for idx, s in enumerate(plan_steps, start=1)}

    registry = get_tool_registry()
    rollback_results: list[dict[str, Any]] = list(state.get("rollback_results") or [])

    logger.warning(
        "Incident %s initiating rollback across %d executed step(s).",
        incident_id,
        len(execution_results),
    )

    for rec in reversed(execution_results):
        if rec.get("status") != "SUCCESS":
            continue
        step_num = rec.get("step", 0)
        tool_name = rec.get("tool_name", "")
        tool_args = dict(rec.get("tool_args") or {})
        step_meta = steps_by_num.get(step_num, {})

        rb_tool_name = step_meta.get("rollback_tool")
        rb_args = dict(step_meta.get("rollback_args") or {})

        # Check NexusTool.get_rollback_action if registered
        if not rb_tool_name:
            nexus_tool = registry.get(tool_name)
            if nexus_tool and hasattr(nexus_tool, "get_rollback_action"):
                rb_spec = nexus_tool.get_rollback_action(**tool_args)
                if isinstance(rb_spec, dict) and rb_spec.get("tool_name"):
                    rb_tool_name = rb_spec["tool_name"]
                    rb_args = dict(rb_spec.get("parameters") or {})

        if not rb_tool_name:
            rollback_results.append(
                {
                    "step": step_num,
                    "original_tool": tool_name,
                    "status": "SKIPPED",
                    "output": f"No inverse rollback tool declared for '{tool_name}'.",
                }
            )
            continue

        rb_tool = registry.get_tool(rb_tool_name)
        if rb_tool is None:
            rollback_results.append(
                {
                    "step": step_num,
                    "original_tool": tool_name,
                    "rollback_tool": rb_tool_name,
                    "status": "FAILED",
                    "output": f"Rollback tool '{rb_tool_name}' not found in registry.",
                }
            )
            continue

        try:
            output = await rb_tool.ainvoke(rb_args)
            rollback_results.append(
                {
                    "step": step_num,
                    "original_tool": tool_name,
                    "rollback_tool": rb_tool_name,
                    "rollback_args": rb_args,
                    "status": "SUCCESS",
                    "output": str(output),
                }
            )
        except Exception as exc:
            logger.error(
                "Incident %s rollback tool %s failed: %s",
                incident_id,
                rb_tool_name,
                exc,
            )
            rollback_results.append(
                {
                    "step": step_num,
                    "original_tool": tool_name,
                    "rollback_tool": rb_tool_name,
                    "status": "FAILED",
                    "output": str(exc),
                }
            )

    return {
        "rollback_results": rollback_results,
        "status": "ROLLED_BACK",
    }
