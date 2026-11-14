"""
Remediation planner for explicit suggested actions.
"""

from __future__ import annotations

import logging
from typing import Any

from nexus.graph.nodes.k8s_plan_helpers import (
    calculate_next_memory_limit,
    calculate_scaled_replicas,
    extract_live_memory_limit,
    extract_live_replicas,
)
from nexus.graph.state import PlanStep

logger = logging.getLogger(__name__)


def build_suggested_action_steps(
    platform: str,
    target_name: str,
    target_ns: str,
    suggested_action: str,
    diagnosis: dict[str, Any],
    state: dict[str, Any],
) -> tuple[list[PlanStep], str | None]:
    """Build steps for an explicit suggested_action from diagnosis."""
    action_params = diagnosis.get("action_params", {}) or {}
    suggested_fix = diagnosis.get("suggested_fix")
    approval_reason = None
    rollback_tool = None
    rollback_params = None

    if suggested_fix:
        approval_reason = suggested_fix
    elif "rollback" in suggested_action or "undo" in suggested_action:
        approval_reason = f"Rollback action: {suggested_action}"
    elif any(
        k in suggested_action
        for k in ("scale", "memory", "timeout", "limit", "command")
    ):
        approval_reason = f"Mutating action: {suggested_action}"

    tool_full_name = suggested_action
    if not (tool_full_name.startswith("k8s_") or tool_full_name.startswith("aws_")):
        prefix = "k8s_" if platform == "kubernetes" else "aws_"
        tool_full_name = f"{prefix}{suggested_action}"

    # Alias normalization for scale
    if tool_full_name in ("k8s_scale_deployment", "k8s_scale"):
        tool_full_name = "k8s_scale_resource"

    params: dict[str, Any] = (
        {"namespace": target_ns, "deployment_name": target_name}
        if platform == "kubernetes"
        else {"function_name": target_name, "region": target_ns}
    )

    if platform == "kubernetes":
        if "scale" in tool_full_name:
            curr_rep = extract_live_replicas(state)
            metrics = state.get("telemetry", {}).get("metrics")
            events = state.get("events")
            scaled_rep = calculate_scaled_replicas(curr_rep, metrics, events)
            params.setdefault("resource_name", target_name)
            params.setdefault("resource_kind", "deployment")
            params.setdefault("replicas", scaled_rep)
            rollback_tool = "k8s_scale_resource"
            rollback_params = {
                "namespace": target_ns,
                "resource_name": target_name,
                "deployment_name": target_name,
                "resource_kind": "deployment",
                "replicas": curr_rep,
            }
        elif "limit" in tool_full_name or "patch_resource_limits" in tool_full_name:
            curr_mem = extract_live_memory_limit(state)
            next_mem = calculate_next_memory_limit(curr_mem)
            params.setdefault("memory_limit", next_mem)
            rollback_tool = "k8s_patch_resource_limits"
            rollback_params = {
                "namespace": target_ns,
                "deployment_name": target_name,
                "memory_limit": curr_mem or "256Mi",
            }
        elif "command" in tool_full_name:
            rollback_tool = "k8s_restart_deployment"
            rollback_params = {"namespace": target_ns, "deployment_name": target_name}
        elif "rollback" in tool_full_name or "restart" in tool_full_name:
            rollback_tool = "k8s_restart_deployment"
            rollback_params = {"namespace": target_ns, "deployment_name": target_name}
    else:
        if "memory" in suggested_action:
            current_mem = (
                state.get("telemetry", {}).get("live_config", {}).get("MemorySize", 256)
            )
            new_mem = min(10240, int(current_mem * 2) if current_mem else 512)
            params.setdefault("memory_mb", new_mem)
            rollback_tool = "aws_update_lambda_memory"
            rollback_params = {
                "function_name": target_name,
                "memory_mb": current_mem,
                "region": target_ns,
            }
        elif "timeout" in suggested_action:
            current_timeout = (
                state.get("telemetry", {}).get("live_config", {}).get("Timeout", 30)
            )
            new_timeout = min(900, int(current_timeout * 2) if current_timeout else 60)
            params.setdefault("timeout_seconds", new_timeout)
            rollback_tool = "aws_update_lambda_timeout"
            rollback_params = {
                "function_name": target_name,
                "timeout_seconds": current_timeout,
                "region": target_ns,
            }
        elif "dlq" in suggested_action:
            params.setdefault("dlq_name", f"{target_name}-dlq")
            params.setdefault("max_messages", 20)

    params.update(action_params)
    step_description = (
        suggested_fix
        or f"Execute dynamic remediation {tool_full_name} on {target_name}"
    )

    step = PlanStep(
        step_index=0,
        tool_name=tool_full_name,
        parameters=params,
        description=step_description,
        rollback_tool=rollback_tool,
        rollback_parameters=rollback_params,
    )
    return [step], approval_reason
