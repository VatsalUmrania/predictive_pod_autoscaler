"""
NEXUS Remediation Planner Node
==============================
Translates the diagnosed root cause and telemetry into a typed, structured
RemediationPlan using registered write tools for the target platform.
Pre-calculates risk tiers and rollback actions deterministically.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from nexus.engine.fsm import IncidentState
from nexus.graph.state import IncidentGraphState, PlanStep, RemediationPlan

logger = logging.getLogger(__name__)


def plan_remediation_node(state: IncidentGraphState) -> dict[str, Any]:
    """Generate structured remediation plan for target platform."""
    incident_id = state["incident_id"]
    diagnosis = state.get("diagnosis", {})
    target = state.get("target", {})
    platform = state.get("platform", "kubernetes")
    failure_class = diagnosis.get("failure_class", "unknown")
    confidence = float(diagnosis.get("confidence", 0.8))

    target_name = target.get("name", "unknown")
    target_ns = target.get("namespace", "default")

    retry_count = state.get("retry_count", 0)
    verification = state.get("verification") or {}
    execution_records = state.get("execution_records") or []
    prior_tools = [rec.get("tool_name") for rec in execution_records]
    verif_failure = verification.get("failure_reason") or verification.get("details") or "Target remained unhealthy"

    steps: list[PlanStep] = []
    requires_approval = False
    approval_reason = None

    suggested_action = diagnosis.get("suggested_action")
    action_params = diagnosis.get("action_params", {}) or {}

    # ── ADAPTIVE RECOVERY LADDER (Retry Feedback Loop) ────────────────────────
    # If a prior remediation step failed post-verification, do NOT blindly repeat
    # the identical action. Escalate along the action ladder (e.g. transient restart
    # failure -> persistent deployment rollback).
    if retry_count > 0 and prior_tools:
        logger.info(
            "[Plan Adaptive Ladder] Retry attempt %d for incident %s. Prior tools: %s. Failure: %s",
            retry_count,
            incident_id,
            prior_tools,
            verif_failure,
        )
        if platform == "kubernetes":
            if failure_class == "resource_exhaustion" or "oom" in diagnosis.get("root_cause", "").lower():
                # For resource exhaustion / OOM, restart failed to relieve pressure; scale to distribute load
                steps.append(
                    PlanStep(
                        step_index=0,
                        tool_name="k8s_scale_deployment",
                        parameters={"namespace": target_ns, "deployment_name": target_name, "replicas": 3},
                        description=f"Adaptive Recovery: Scale {target_name} to 3 replicas to distribute load after restart failed to clear resource pressure",
                        rollback_tool="k8s_scale_deployment",
                        rollback_parameters={"namespace": target_ns, "deployment_name": target_name, "replicas": 1},
                    )
                )
                approval_reason = f"Adaptive Recovery: Scale {target_name} to 3 replicas after initial restart failed"
            elif any("restart" in t for t in prior_tools):
                # Prior restart failed to clear the crashloop; problem is persistent code/config defect
                steps.append(
                    PlanStep(
                        step_index=0,
                        tool_name="k8s_rollback_deployment",
                        parameters={"namespace": target_ns, "deployment_name": target_name},
                        description=f"Adaptive Recovery: Rollback {target_name} to previous revision after rollout restart failed to clear failure",
                        rollback_tool="k8s_restart_deployment",
                        rollback_parameters={"namespace": target_ns, "deployment_name": target_name},
                    )
                )
                approval_reason = (
                    f"Adaptive Recovery: Attempt {retry_count} ({prior_tools[-1]}) failed post-check: "
                    f"'{verif_failure}'. Escalating to deployment rollback."
                )
            elif any("scale" in t for t in prior_tools):
                steps.append(
                    PlanStep(
                        step_index=0,
                        tool_name="k8s_rollback_deployment",
                        parameters={"namespace": target_ns, "deployment_name": target_name},
                        description=f"Adaptive Recovery: Rollback {target_name} after scaling failed to restore health",
                    )
                )
                approval_reason = f"Adaptive Recovery: Prior scaling failed ('{verif_failure}'). Escalating to rollback."
            else:
                steps.append(
                    PlanStep(
                        step_index=0,
                        tool_name="k8s_rollback_deployment",
                        parameters={"namespace": target_ns, "deployment_name": target_name},
                        description=f"Adaptive Recovery: Escalating to rollback after attempt {retry_count} failed",
                    )
                )
                approval_reason = f"Adaptive Recovery: Attempt {retry_count} failed verification ('{verif_failure}'). Escalating to rollback."

        elif platform == "aws":
            if any("memory" in t for t in prior_tools):
                if any(w in verif_failure.lower() for w in ("timeout", "timed out", "duration")):
                    current_timeout = state.get("telemetry", {}).get("live_config", {}).get("Timeout", 30)
                    new_timeout = min(900, int(current_timeout * 2) if current_timeout else 60)
                    steps.append(
                        PlanStep(
                            step_index=0,
                            tool_name="aws_update_lambda_timeout",
                            parameters={"function_name": target_name, "timeout_seconds": new_timeout, "region": target_ns},
                            description=f"Adaptive Recovery: Increase timeout to {new_timeout}s after memory increase failed",
                        )
                    )
                    approval_reason = f"Adaptive Recovery: Memory increase failed ('{verif_failure}'). Escalating to timeout increase."
                else:
                    steps.append(
                        PlanStep(
                            step_index=0,
                            tool_name="aws_rollback_lambda_alias",
                            parameters={"function_name": target_name, "alias_name": "live", "region": target_ns},
                            description=f"Adaptive Recovery: Rollback Lambda alias for {target_name} after memory increase failed",
                        )
                    )
                    approval_reason = f"Adaptive Recovery: Memory increase failed ('{verif_failure}'). Escalating to alias rollback."
            else:
                steps.append(
                    PlanStep(
                        step_index=0,
                        tool_name="aws_rollback_lambda_alias",
                        parameters={"function_name": target_name, "alias_name": "live", "region": target_ns},
                        description=f"Adaptive Recovery: Rollback Lambda alias after attempt {retry_count} failed",
                    )
                )
                approval_reason = f"Adaptive Recovery: Attempt {retry_count} failed ('{verif_failure}'). Escalating to alias rollback."


    elif suggested_action:
        # Dynamic tool proposal from diagnosis / evidence
        rollback_tool = None
        rollback_params = None
        if "rollback" in suggested_action or "undo" in suggested_action:
            approval_reason = f"Rollback action: {suggested_action}"
            rollback_tool = "k8s_restart_deployment" if platform == "kubernetes" else None
            rollback_params = {"namespace": target_ns, "deployment_name": target_name} if platform == "kubernetes" else None
        elif "scale" in suggested_action or "memory" in suggested_action or "timeout" in suggested_action:
            approval_reason = f"Mutating action: {suggested_action}"
        elif "restart" in suggested_action:
            rollback_tool = "k8s_restart_deployment" if platform == "kubernetes" else None
            rollback_params = {"namespace": target_ns, "deployment_name": target_name} if platform == "kubernetes" else None

        tool_full_name = suggested_action
        if not (tool_full_name.startswith("k8s_") or tool_full_name.startswith("aws_")):
            prefix = "k8s_" if platform == "kubernetes" else "aws_"
            tool_full_name = f"{prefix}{suggested_action}"

        params = {"namespace": target_ns, "deployment_name": target_name} if platform == "kubernetes" else {"function_name": target_name, "region": target_ns}
        if "memory" in suggested_action:
            current_mem = state.get("telemetry", {}).get("live_config", {}).get("MemorySize", 256)
            new_mem = min(10240, int(current_mem * 2) if current_mem else 512)
            params.setdefault("memory_mb", new_mem)
            rollback_tool = "aws_update_lambda_memory"
            rollback_params = {"function_name": target_name, "memory_mb": current_mem, "region": target_ns}
        elif "timeout" in suggested_action:
            current_timeout = state.get("telemetry", {}).get("live_config", {}).get("Timeout", 30)
            new_timeout = min(900, int(current_timeout * 2) if current_timeout else 60)
            params.setdefault("timeout_seconds", new_timeout)
            rollback_tool = "aws_update_lambda_timeout"
            rollback_params = {"function_name": target_name, "timeout_seconds": current_timeout, "region": target_ns}
        elif "dlq" in suggested_action:
            params.setdefault("dlq_name", f"{target_name}-dlq")
            params.setdefault("max_messages", 20)

        params.update(action_params)

        steps.append(
            PlanStep(
                step_index=0,
                tool_name=tool_full_name,
                parameters=params,
                description=f"Execute dynamic remediation {tool_full_name} on {target_name}",
                rollback_tool=rollback_tool,
                rollback_parameters=rollback_params,
            )
        )
    elif platform == "kubernetes":
        if failure_class == "bad_deploy":
            steps.append(
                PlanStep(
                    step_index=0,
                    tool_name="k8s_rollback_deployment",
                    parameters={"namespace": target_ns, "deployment_name": target_name},
                    description=f"Rollback deployment {target_name} to previous stable revision",
                    rollback_tool="k8s_restart_deployment",
                    rollback_parameters={"namespace": target_ns, "deployment_name": target_name},
                )
            )
            approval_reason = "Rollback deployment"
        elif failure_class == "resource_exhaustion":
            # Scale or patch limits
            steps.append(
                PlanStep(
                    step_index=0,
                    tool_name="k8s_restart_deployment",
                    parameters={"namespace": target_ns, "deployment_name": target_name},
                    description=f"Rollout restart {target_name} to clear transient resource exhaustion",
                    rollback_tool="k8s_restart_deployment",
                    rollback_parameters={"namespace": target_ns, "deployment_name": target_name},
                )
            )
        else:
            # Default safe restart for transient/dependency issues
            steps.append(
                PlanStep(
                    step_index=0,
                    tool_name="k8s_restart_deployment",
                    parameters={"namespace": target_ns, "deployment_name": target_name},
                    description=f"Restart pods of {target_name} to restore healthy state",
                )
            )

    elif platform == "aws":
        if failure_class == "resource_exhaustion" or "oom" in diagnosis.get("root_cause", "").lower():
            # Lambda memory increase
            current_mem = state.get("telemetry", {}).get("live_config", {}).get("MemorySize", 256)
            new_mem = min(10240, int(current_mem * 2) if current_mem else 512)
            steps.append(
                PlanStep(
                    step_index=0,
                    tool_name="aws_update_lambda_memory",
                    parameters={"function_name": target_name, "memory_mb": new_mem, "region": target_ns},
                    description=f"Increase Lambda memory from {current_mem}MB to {new_mem}MB",
                    rollback_tool="aws_update_lambda_memory",
                    rollback_parameters={"function_name": target_name, "memory_mb": current_mem, "region": target_ns},
                )
            )
            approval_reason = f"Increase Lambda memory to {new_mem}MB"
        elif "timeout" in diagnosis.get("root_cause", "").lower():
            # Lambda timeout increase
            current_timeout = state.get("telemetry", {}).get("live_config", {}).get("Timeout", 30)
            new_timeout = min(900, int(current_timeout * 2) if current_timeout else 60)
            steps.append(
                PlanStep(
                    step_index=0,
                    tool_name="aws_update_lambda_timeout",
                    parameters={"function_name": target_name, "timeout_seconds": new_timeout, "region": target_ns},
                    description=f"Increase Lambda timeout from {current_timeout}s to {new_timeout}s",
                    rollback_tool="aws_update_lambda_timeout",
                    rollback_parameters={"function_name": target_name, "timeout_seconds": current_timeout, "region": target_ns},
                )
            )
            approval_reason = f"Increase Lambda timeout to {new_timeout}s"
        elif failure_class == "bad_deploy":
            steps.append(
                PlanStep(
                    step_index=0,
                    tool_name="aws_rollback_lambda_alias",
                    parameters={"function_name": target_name, "alias_name": "live", "region": target_ns},
                    description=f"Roll back Lambda alias for {target_name}",
                )
            )
            approval_reason = "Rollback Lambda alias"
        else:
            # DLQ replay or safe observe
            steps.append(
                PlanStep(
                    step_index=0,
                    tool_name="aws_replay_sqs_dlq",
                    parameters={"dlq_name": f"{target_name}-dlq", "max_messages": 20, "region": target_ns},
                    description=f"Replay poison messages from DLQ for {target_name}",
                )
            )
    else:
        # Future / Pluggable platform: dynamically introspect tools from adapter!
        from nexus.graph.platform import get_platform_registry

        registry = get_platform_registry()
        adapter = registry.get(platform)
        available_tools = adapter.get_tools() if adapter else []

        chosen_tool = None
        for t in available_tools:
            name_lower = getattr(t, "name", "").lower()
            if "get" not in name_lower and "describe" not in name_lower and "list" not in name_lower:
                chosen_tool = t
                break
        if not chosen_tool and available_tools:
            chosen_tool = available_tools[0]

        if chosen_tool:
            schema_props = (
                chosen_tool.args_schema.model_json_schema().get("properties", {})
                if hasattr(chosen_tool, "args_schema")
                else {}
            )
            params = {}
            for p in schema_props:
                p_lower = p.lower()
                if "target" in p_lower or "name" in p_lower or "instance" in p_lower:
                    params[p] = target_name
                elif "project" in p_lower:
                    uri = target.get("arn_or_uri", "")
                    params[p] = uri.split("/")[1] if "/" in uri else "default-project"
                elif "zone" in p_lower or "region" in p_lower or "namespace" in p_lower:
                    params[p] = target_ns
                else:
                    params[p] = "default"

            steps.append(
                PlanStep(
                    step_index=0,
                    tool_name=chosen_tool.name,
                    parameters=params,
                    description=f"Remediate {target_name} using {chosen_tool.name}",
                )
            )
        else:
            steps.append(
                PlanStep(
                    step_index=0,
                    tool_name="generic_remediate",
                    parameters={"target": target_name},
                    description=f"Execute default remediation on platform {platform}",
                )
            )

    # Universal Human Approval Enforcement:
    # All remediation actions require human authorization before execution.
    if steps:
        requires_approval = True
        approval_reason = approval_reason or f"Human authorization required to execute {steps[0].tool_name} on {target_name}"
    else:
        requires_approval = False

    plan = RemediationPlan(
        incident_id=incident_id,
        root_cause=diagnosis.get("root_cause", "Unspecified failure"),
        failure_mode=failure_class,
        confidence=confidence,
        steps=steps,
        requires_approval=requires_approval,
        approval_reason=approval_reason,
        pre_flight_conditions={"target_exists": True},
        expected_slo_target="error_rate < 0.01",
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    audit_entry = {
        "timestamp": now_iso,
        "stage": "plan",
        "action": "plan_generated",
        "details": f"Steps: {len(steps)} | Requires Approval: {requires_approval} ({approval_reason or 'autonomous'})",
    }

    logger.info(
        "[Plan] Incident %s planned: %d step(s), approval=%s",
        incident_id,
        len(steps),
        requires_approval,
    )

    remediation_entry = {
        "attempt": retry_count,
        "prior_tools": prior_tools,
        "post_check_failure": verif_failure if retry_count > 0 else None,
        "proposed_steps": [s.model_dump() for s in steps],
        "is_adaptive_escalation": retry_count > 0,
    }

    result_dict = {
        "plan": plan.model_dump(),
        "remediation_history": [remediation_entry],
        "fsm_state": IncidentState.POLICY_CHECK.value,
        "audit_log": [audit_entry],
    }
    if retry_count > 0:
        result_dict["approval_decision"] = None
    return result_dict

