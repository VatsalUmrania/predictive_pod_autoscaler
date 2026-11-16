"""
Plan formatting utilities for NEXUS planning nodes.
"""

from __future__ import annotations

import re
from typing import Any


def double_memory_limit(cur: str) -> str:
    m = re.match(r"^(\d+)([A-Za-z]+)$", cur.strip())
    if m:
        return f"{int(m.group(1)) * 2}{m.group(2)}"
    return "512Mi"


def format_plan(
    tool: str,
    params: dict[str, Any],
    rb_tool: str,
    rb_params: dict[str, Any],
    v_tool: str,
    t_name: str,
    approval_reason: str = "",
) -> dict[str, Any]:
    step = {
        "step_index": 0,
        "tool_name": tool,
        "parameters": params,
        "rollback_tool": rb_tool,
        "rollback_parameters": rb_params,
        "verification_tool": v_tool,
        "verification_parameters": params,
        "verification_condition": {
            "metric_name": "ready_replicas",
            "operator": ">=",
            "threshold": 1.0,
            "window_seconds": 60,
        },
    }
    plan = {
        "summary": f"Plan for {t_name}",
        "steps": [step],
        "actions": [
            {
                "step_number": 0,
                "tool_name": tool,
                "tool_arguments": params,
                "rollback_tool_name": rb_tool,
                "rollback_arguments": rb_params,
            }
        ],
        "verification_checks": [
            {
                "tool_name": v_tool,
                "tool_arguments": params,
                "expected_healthy_condition": "ready_replicas >= 1.0",
            }
        ],
        "requires_approval": True,
    }
    if approval_reason:
        plan["approval_reason"] = approval_reason
    return plan
