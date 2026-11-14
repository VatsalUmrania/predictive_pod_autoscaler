"""
NEXUS Remediation Planner Node
==============================
Translates the diagnosed root cause and telemetry into a typed, structured
RemediationPlan using registered write tools for the target platform.
Pre-calculates risk tiers and rollback actions deterministically.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from nexus.engine.fsm import IncidentState
from nexus.graph.llm import get_llm
from nexus.graph.nodes.action_planner import build_suggested_action_steps
from nexus.graph.nodes.adaptive_recovery import build_adaptive_recovery_steps
from nexus.graph.nodes.default_planner import (
    build_aws_default_steps,
    build_k8s_default_steps,
    build_pluggable_platform_steps,
)
from nexus.graph.rca.prompts import REMEDIATION_PLANNER_SYSTEM_PROMPT
from nexus.graph.state import (
    IncidentGraphState,
    PlanStep,
    RemediationPlan,
    VerificationCondition,
)
from nexus.tools.registry import default_registry

logger = logging.getLogger(__name__)


def _plan_deterministic(
    platform: str,
    target_name: str,
    target_ns: str,
    failure_class: str,
    suggested_action: str | None,
    diagnosis: dict[str, Any],
    state: dict[str, Any],
    target: dict[str, Any],
) -> tuple[list[PlanStep], str | None]:
    """Execute deterministic fallback planning using rule-based templates."""
    if suggested_action:
        return build_suggested_action_steps(
            platform=platform,
            target_name=target_name,
            target_ns=target_ns,
            suggested_action=suggested_action,
            diagnosis=diagnosis,
            state=state,
        )
    elif platform == "kubernetes":
        return build_k8s_default_steps(
            target_name=target_name,
            target_ns=target_ns,
            failure_class=failure_class,
            diagnosis=diagnosis,
            state=state,
        )
    elif platform == "aws":
        return build_aws_default_steps(
            target_name=target_name,
            target_ns=target_ns,
            failure_class=failure_class,
            diagnosis=diagnosis,
            state=state,
        )
    else:
        return build_pluggable_platform_steps(
            platform=platform,
            target_name=target_name,
            target_ns=target_ns,
            target=target,
        )


def _plan_with_llm_or_fallback(
    state: IncidentGraphState,
    incident_id: str,
    platform: str,
    target_name: str,
    target_ns: str,
    failure_class: str,
    suggested_action: str | None,
    diagnosis: dict[str, Any],
    target: dict[str, Any],
) -> tuple[list[PlanStep], str | None, str]:
    """
    Attempt autonomous LLM planning producing typed PlanSteps and VerificationConditions.
    Falls back to deterministic rule-based planning on failure or unparseable output.
    """
    default_slo = "error_rate < 0.01"

    try:
        llm = get_llm()

        platform_prefix = "k8s_" if platform == "kubernetes" else "aws_"
        platform_tools = [
            t.name
            for t in default_registry.list_tools()
            if t.name.startswith(platform_prefix)
        ]
        all_remediation_tools = [
            t.name for t in default_registry.get_remediation_tools()
        ]

        prompt_payload = {
            "incident_id": incident_id,
            "platform": platform,
            "target": {"name": target_name, "namespace": target_ns},
            "failure_class": failure_class,
            "diagnosis": diagnosis,
            "telemetry": state.get("telemetry", {}),
            "available_tools": platform_tools or all_remediation_tools,
        }

        messages = [
            SystemMessage(content=REMEDIATION_PLANNER_SYSTEM_PROMPT),
            HumanMessage(content=json.dumps(prompt_payload, default=str)),
        ]

        response = llm.invoke(messages)
        raw_text = response.content if hasattr(response, "content") else str(response)

        if "```" in raw_text:
            match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw_text)
            if match:
                raw_text = match.group(1)

        data = json.loads(raw_text.strip())
        raw_steps = data.get("steps")
        if not raw_steps or not isinstance(raw_steps, list):
            raise ValueError("LLM returned no remediation steps")

        steps: list[PlanStep] = []
        for idx, s in enumerate(raw_steps):
            tool_name = s.get("tool_name") or s.get("action_tool")
            if not tool_name:
                continue
            if not default_registry.get(tool_name):
                logger.warning(
                    "[Plan LLM] Proposed tool '%s' is not registered in ToolRegistry; rejecting LLM plan",
                    tool_name,
                )
                raise ValueError(f"Unregistered tool proposed: {tool_name}")

            vc_dict = s.get("verification_condition")
            v_cond = (
                VerificationCondition(**vc_dict) if isinstance(vc_dict, dict) else None
            )

            step = PlanStep(
                step_index=idx,
                tool_name=tool_name,
                parameters=s.get("parameters") or s.get("action_args") or {},
                description=s.get("description", f"Execute {tool_name}"),
                rollback_tool=s.get("rollback_tool"),
                rollback_parameters=s.get("rollback_parameters")
                or s.get("rollback_args"),
                verification_tool=s.get("verification_tool"),
                verification_parameters=s.get("verification_parameters")
                or s.get("verification_args")
                or {},
                verification_condition=v_cond,
            )
            steps.append(step)

        if not steps:
            raise ValueError("No valid PlanSteps parsed from LLM output")

        approval_reason = data.get("approval_reason")
        expected_slo = data.get("expected_slo_target", default_slo)
        logger.info(
            "[Plan LLM] Successfully generated autonomous plan with %d step(s)",
            len(steps),
        )
        return steps, approval_reason, expected_slo

    except Exception as exc:
        logger.warning(
            "[Plan LLM] Autonomous planning was unavailable or failed (%s). Falling back to deterministic planner.",
            exc,
        )
        steps, approval_reason = _plan_deterministic(
            platform=platform,
            target_name=target_name,
            target_ns=target_ns,
            failure_class=failure_class,
            suggested_action=suggested_action,
            diagnosis=diagnosis,
            state=state,
            target=target,
        )
        return steps, approval_reason, default_slo


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
    prior_tools: list[str] = [
        str(rec["tool_name"]) for rec in execution_records if rec.get("tool_name")
    ]
    verif_failure = (
        verification.get("failure_reason")
        or verification.get("details")
        or "Target remained unhealthy"
    )

    suggested_action = diagnosis.get("suggested_action")
    fallback_active = state.get("fallback_active", False)

    steps: list[PlanStep] = []
    approval_reason: str | None = None
    expected_slo_target: str = "error_rate < 0.01"

    # 1. Adaptive recovery ladder for retries
    if retry_count > 0 and prior_tools:
        logger.info(
            "[Plan Adaptive Ladder] Retry attempt %d for incident %s. Prior tools: %s. Failure: %s",
            retry_count,
            incident_id,
            prior_tools,
            verif_failure,
        )
        steps, approval_reason = build_adaptive_recovery_steps(
            platform=platform,
            failure_class=failure_class,
            target_name=target_name,
            target_ns=target_ns,
            retry_count=retry_count,
            prior_tools=prior_tools,
            verif_failure=verif_failure,
            diagnosis=diagnosis,
            state=state,
        )
    elif fallback_active:
        logger.info(
            "[Plan Fallback] Deterministic planner activated for incident %s (platform: %s)",
            incident_id,
            platform,
        )
        steps, approval_reason = _plan_deterministic(
            platform=platform,
            target_name=target_name,
            target_ns=target_ns,
            failure_class=failure_class,
            suggested_action=suggested_action,
            diagnosis=diagnosis,
            state=state,
            target=target,
        )
    else:
        steps, approval_reason, expected_slo_target = _plan_with_llm_or_fallback(
            state=state,
            incident_id=incident_id,
            platform=platform,
            target_name=target_name,
            target_ns=target_ns,
            failure_class=failure_class,
            suggested_action=suggested_action,
            diagnosis=diagnosis,
            target=target,
        )

    # Safety net: ensure every step has verification_tool and verification_condition
    for idx, s in enumerate(steps):
        s.step_index = idx
        if not s.verification_tool:
            if platform == "kubernetes":
                s.verification_tool = "k8s_describe_resource"
                s.verification_parameters = {
                    "namespace": target_ns,
                    "resource_kind": "deployment",
                    "resource_name": target_name,
                }
            else:
                s.verification_tool = "aws_get_metric_data"
                s.verification_parameters = {
                    "namespace": "AWS/Lambda",
                    "metric_name": "Errors",
                    "dimension_name": "FunctionName",
                    "dimension_value": target_name,
                }
        if not s.verification_condition:
            if platform == "kubernetes":
                s.verification_condition = VerificationCondition(
                    metric_name="ready_replicas",
                    operator=">=",
                    threshold=1.0,
                    window_seconds=60,
                )
            else:
                s.verification_condition = VerificationCondition(
                    metric_name="Errors",
                    operator="<=",
                    threshold=0.0,
                    window_seconds=60,
                )

    requires_approval = bool(steps)
    if requires_approval and not approval_reason:
        approval_reason = f"Human authorization required to execute {steps[0].tool_name} on {target_name}"

    plan = RemediationPlan(
        incident_id=incident_id,
        root_cause=diagnosis.get("root_cause", "Unspecified failure"),
        failure_mode=failure_class,
        confidence=confidence,
        steps=steps,
        requires_approval=requires_approval,
        approval_reason=approval_reason,
        pre_flight_conditions={"target_exists": True},
        expected_slo_target=expected_slo_target,
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

    result_dict: dict[str, Any] = {
        "plan": plan.model_dump(),
        "remediation_history": [remediation_entry],
        "fsm_state": IncidentState.POLICY_CHECK.value,
        "audit_log": [audit_entry],
    }
    if retry_count > 0:
        result_dict["approval_decision"] = None
    return result_dict
