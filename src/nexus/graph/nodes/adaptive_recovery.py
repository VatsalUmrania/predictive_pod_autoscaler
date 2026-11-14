"""
Adaptive Recovery Ladder for retry attempts.
"""

from __future__ import annotations

import logging
from typing import Any

from nexus.graph.state import PlanStep

logger = logging.getLogger(__name__)


def build_adaptive_recovery_steps(
    platform: str,
    failure_class: str,
    target_name: str,
    target_ns: str,
    retry_count: int,
    prior_tools: list[str],
    verif_failure: str,
    diagnosis: dict[str, Any],
    state: dict[str, Any],
) -> tuple[list[PlanStep], str | None]:
    """Generate adaptive escalation steps when prior attempts failed verification."""
    steps: list[PlanStep] = []
    approval_reason: str | None = None

    if platform == "kubernetes":
        if (
            failure_class == "resource_exhaustion"
            or "oom" in diagnosis.get("root_cause", "").lower()
        ):
            steps.append(
                PlanStep(
                    step_index=0,
                    tool_name="k8s_scale_deployment",
                    parameters={
                        "namespace": target_ns,
                        "deployment_name": target_name,
                        "replicas": 3,
                    },
                    description=f"Adaptive Recovery: Scale {target_name} to 3 replicas to distribute load after restart failed to clear resource pressure",
                    rollback_tool="k8s_scale_deployment",
                    rollback_parameters={
                        "namespace": target_ns,
                        "deployment_name": target_name,
                        "replicas": 1,
                    },
                )
            )
            approval_reason = f"Adaptive Recovery: Scale {target_name} to 3 replicas after initial restart failed"
        elif any("restart" in t for t in prior_tools):
            steps.append(
                PlanStep(
                    step_index=0,
                    tool_name="k8s_rollback_deployment",
                    parameters={"namespace": target_ns, "deployment_name": target_name},
                    description=f"Adaptive Recovery: Rollback {target_name} to previous revision after rollout restart failed to clear failure",
                    rollback_tool="k8s_restart_deployment",
                    rollback_parameters={
                        "namespace": target_ns,
                        "deployment_name": target_name,
                    },
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
            if any(
                w in verif_failure.lower() for w in ("timeout", "timed out", "duration")
            ):
                current_timeout = (
                    state.get("telemetry", {}).get("live_config", {}).get("Timeout", 30)
                )
                new_timeout = min(
                    900, int(current_timeout * 2) if current_timeout else 60
                )
                steps.append(
                    PlanStep(
                        step_index=0,
                        tool_name="aws_update_lambda_timeout",
                        parameters={
                            "function_name": target_name,
                            "timeout_seconds": new_timeout,
                            "region": target_ns,
                        },
                        description=f"Adaptive Recovery: Increase timeout to {new_timeout}s after memory increase failed",
                    )
                )
                approval_reason = f"Adaptive Recovery: Memory increase failed ('{verif_failure}'). Escalating to timeout increase."
            else:
                steps.append(
                    PlanStep(
                        step_index=0,
                        tool_name="aws_rollback_lambda_alias",
                        parameters={
                            "function_name": target_name,
                            "alias_name": "live",
                            "region": target_ns,
                        },
                        description=f"Adaptive Recovery: Rollback Lambda alias for {target_name} after memory increase failed",
                    )
                )
                approval_reason = f"Adaptive Recovery: Memory increase failed ('{verif_failure}'). Escalating to alias rollback."
        else:
            steps.append(
                PlanStep(
                    step_index=0,
                    tool_name="aws_rollback_lambda_alias",
                    parameters={
                        "function_name": target_name,
                        "alias_name": "live",
                        "region": target_ns,
                    },
                    description=f"Adaptive Recovery: Rollback Lambda alias after attempt {retry_count} failed",
                )
            )
            approval_reason = f"Adaptive Recovery: Attempt {retry_count} failed ('{verif_failure}'). Escalating to alias rollback."

    return steps, approval_reason
