"""
Default platform planners for K8s, AWS, and pluggable platforms.
"""

from __future__ import annotations

import logging
from typing import Any

from nexus.graph.nodes.k8s_plan_helpers import (
    calculate_next_memory_limit,
    calculate_scaled_replicas,
    detect_faulty_command,
    detect_oom_issue,
    detect_traffic_spike,
    extract_live_memory_limit,
    extract_live_replicas,
)
from nexus.graph.nodes.pluggable_planner import build_pluggable_platform_steps
from nexus.graph.state import PlanStep

logger = logging.getLogger(__name__)


def build_k8s_default_steps(
    target_name: str,
    target_ns: str,
    failure_class: str,
    diagnosis: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
) -> tuple[list[PlanStep], str | None]:
    """Default remediation steps for Kubernetes computed dynamically from live context."""
    diagnosis = diagnosis or {}
    state = state or {}

    # 1. Faulty command override detection
    is_faulty, container = detect_faulty_command(state)
    if is_faulty:
        params: dict[str, Any] = {"namespace": target_ns, "deployment_name": target_name}
        if container:
            params["container_name"] = container
        return [
            PlanStep(
                step_index=0,
                tool_name="k8s_remove_container_command",
                parameters=params,
                description=f"Remove faulty container command override from {target_name} to restore entrypoint",
                rollback_tool="k8s_restart_deployment",
                rollback_parameters={"namespace": target_ns, "deployment_name": target_name},
            )
        ], "Remove container command override"

    # 2. OOMKilled / memory limit exhaustion
    if detect_oom_issue(state, diagnosis):
        curr_mem = extract_live_memory_limit(state)
        next_mem = calculate_next_memory_limit(curr_mem)
        return [
            PlanStep(
                step_index=0,
                tool_name="k8s_patch_resource_limits",
                parameters={"namespace": target_ns, "deployment_name": target_name, "memory_limit": next_mem},
                description=f"Patch memory limit for {target_name} from {curr_mem or 'default'} to {next_mem}",
                rollback_tool="k8s_patch_resource_limits",
                rollback_parameters={"namespace": target_ns, "deployment_name": target_name, "memory_limit": curr_mem or "256Mi"},
            )
        ], f"Patch memory limit to {next_mem}"

    # 3. Traffic surge / cascading failure
    if detect_traffic_spike(state, diagnosis):
        curr_rep = extract_live_replicas(state)
        scaled_rep = calculate_scaled_replicas(curr_rep, state.get("telemetry", {}).get("metrics"), state.get("events"))
        return [
            PlanStep(
                step_index=0,
                tool_name="k8s_scale_resource",
                parameters={"namespace": target_ns, "resource_name": target_name, "deployment_name": target_name, "replicas": scaled_rep, "resource_kind": "deployment"},
                description=f"Scale {target_name} from {curr_rep} to {scaled_rep} replicas to mitigate traffic surge",
                rollback_tool="k8s_scale_resource",
                rollback_parameters={"namespace": target_ns, "resource_name": target_name, "deployment_name": target_name, "replicas": curr_rep, "resource_kind": "deployment"},
            )
        ], f"Scale deployment to {scaled_rep} replicas"

    # 4. Bad deployment rollout regression
    if failure_class == "bad_deploy":
        return [
            PlanStep(
                step_index=0,
                tool_name="k8s_rollback_deployment",
                parameters={"namespace": target_ns, "deployment_name": target_name},
                description=f"Rollback deployment {target_name} to previous stable revision",
                rollback_tool="k8s_restart_deployment",
                rollback_parameters={"namespace": target_ns, "deployment_name": target_name},
            )
        ], "Rollback deployment"

    # 5. Fallback restart for transient deadlock/resource exhaustion
    return [
        PlanStep(
            step_index=0,
            tool_name="k8s_restart_deployment",
            parameters={"namespace": target_ns, "deployment_name": target_name},
            description=f"Restart pods of {target_name} to restore healthy state",
            rollback_tool="k8s_restart_deployment",
            rollback_parameters={"namespace": target_ns, "deployment_name": target_name},
        )
    ], None


def build_aws_default_steps(
    target_name: str,
    target_ns: str,
    failure_class: str,
    diagnosis: dict[str, Any],
    state: dict[str, Any],
) -> tuple[list[PlanStep], str | None]:
    """Default remediation steps for AWS."""
    root_cause = diagnosis.get("root_cause", "").lower()
    if failure_class == "resource_exhaustion" or "oom" in root_cause:
        current_mem = state.get("telemetry", {}).get("live_config", {}).get("MemorySize", 256)
        new_mem = min(10240, int(current_mem * 2) if current_mem else 512)
        return [
            PlanStep(
                step_index=0,
                tool_name="aws_update_lambda_memory",
                parameters={"function_name": target_name, "memory_mb": new_mem, "region": target_ns},
                description=f"Increase Lambda memory from {current_mem}MB to {new_mem}MB",
                rollback_tool="aws_update_lambda_memory",
                rollback_parameters={"function_name": target_name, "memory_mb": current_mem, "region": target_ns},
            )
        ], f"Increase Lambda memory to {new_mem}MB"
    if "timeout" in root_cause:
        current_timeout = state.get("telemetry", {}).get("live_config", {}).get("Timeout", 30)
        new_timeout = min(900, int(current_timeout * 2) if current_timeout else 60)
        return [
            PlanStep(
                step_index=0,
                tool_name="aws_update_lambda_timeout",
                parameters={"function_name": target_name, "timeout_seconds": new_timeout, "region": target_ns},
                description=f"Increase Lambda timeout from {current_timeout}s to {new_timeout}s",
                rollback_tool="aws_update_lambda_timeout",
                rollback_parameters={"function_name": target_name, "timeout_seconds": current_timeout, "region": target_ns},
            )
        ], f"Increase Lambda timeout to {new_timeout}s"
    if failure_class == "bad_deploy":
        return [
            PlanStep(
                step_index=0,
                tool_name="aws_rollback_lambda_alias",
                parameters={"function_name": target_name, "alias_name": "live", "region": target_ns},
                description=f"Roll back Lambda alias for {target_name}",
            )
        ], "Rollback Lambda alias"
    return [
        PlanStep(
            step_index=0,
            tool_name="aws_replay_sqs_dlq",
            parameters={"dlq_name": f"{target_name}-dlq", "max_messages": 20, "region": target_ns},
            description=f"Replay poison messages from DLQ for {target_name}",
        )
    ], None


__all__ = [
    "build_k8s_default_steps",
    "build_aws_default_steps",
    "build_pluggable_platform_steps",
]

