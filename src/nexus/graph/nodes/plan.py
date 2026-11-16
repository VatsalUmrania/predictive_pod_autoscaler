"""
NEXUS Schema-Driven Remediation Planner Node
============================================
Synthesizes atomic remediation plans with pre-state rollback guarantees
and read-only verification checks using registered mutating tool schemas.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from nexus.db.models import IncidentState
from nexus.graph.llm import get_llm
from nexus.graph.nodes.plan_formatter import double_memory_limit, format_plan
from nexus.tools.registry import ToolRegistry, default_registry

logger = logging.getLogger(__name__)


class PlannedToolAction(BaseModel):
    step_number: int
    tool_name: str = Field(description="Registered mutating tool name")
    tool_arguments: dict[str, Any] = Field(default_factory=dict)
    target_resource_urn: str = ""
    rationale: str = ""
    action_category: Literal[
        "restart", "scale", "config_patch", "rollback", "traffic_shift", "destructive"
    ] = "restart"
    blast_radius: Literal[
        "pod_or_instance", "service", "cluster_or_vpc", "stateful_data"
    ] = "pod_or_instance"
    reversible: bool = True
    rollback_tool_name: str = ""
    rollback_arguments: dict[str, Any] = Field(default_factory=dict)


class VerificationCheck(BaseModel):
    tool_name: str = Field(description="Read-only tool to verify health")
    tool_arguments: dict[str, Any] = Field(default_factory=dict)
    expected_healthy_condition: str = ""


class RemediationPlan(BaseModel):
    summary: str = ""
    actions: list[PlannedToolAction] = Field(default_factory=list)
    verification_checks: list[VerificationCheck] = Field(default_factory=list)
    estimated_cost_delta_pct: float = 0.0
    stabilization_wait_seconds: int = 30


PLANNER_PROMPT = """You are the NEXUS Autonomous SRE Remediation Planner.
1. Compose minimal mutating tool calls (`actions`) to resolve root cause.
2. For EVERY action, populate `rollback_tool_name` and `rollback_arguments`.
3. Define 1-3 `verification_checks` using ONLY read-only tools."""


def _build_deterministic_plan(
    state: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
    target = state.get("target") or {}
    t_name = (
        target.get("name", "service")
        if isinstance(target, dict)
        else getattr(target, "name", "service")
    )
    t_ns = (
        target.get("namespace", "default")
        if isinstance(target, dict)
        else getattr(target, "namespace", "default")
    )
    diag = state.get("diagnosis") or {}
    prov = state.get("platform") or "kubernetes"
    dep_name = (
        re.sub(r"-[0-9a-f]{8,10}-[0-9a-z]{5}$", "", t_name)
        if prov == "kubernetes"
        else t_name
    )
    telem = state.get("telemetry") or {}
    live_cfg = telem.get("live_config") or {}
    metrics = telem.get("metrics") or {}
    events = state.get("events") or []
    root_cause = str(diag.get("root_cause", "")).lower()
    fail_cls = str(diag.get("failure_class", "")).lower()
    suggested = diag.get("suggested_action")
    records = state.get("execution_records") or []
    prior_tools = [r.get("tool_name") for r in records if r.get("tool_name")]
    retry_count = state.get("retry_count", 0)
    verif = state.get("verification") or {}
    verif_reason = str(verif.get("failure_reason", "")).lower()

    history: list[dict[str, Any]] | None = None
    approval_reason = ""

    if retry_count > 0 or not verif.get("healthy", True) and prior_tools:
        approval_reason = f"Adaptive Recovery: Prior remediation ({', '.join(prior_tools) or 'attempt'}) failed post-checks; escalating."
        history = [
            {
                "is_adaptive_escalation": True,
                "attempt": retry_count or 1,
                "prior_tools": prior_tools,
            }
        ]
        if prov == "aws" and (
            "aws_update_lambda_memory" in prior_tools
            or "timed out" in verif_reason
            or "duration" in root_cause
        ):
            cur_to = int(live_cfg.get("Timeout", 30))
            tool = "aws_update_lambda_timeout"
            params = {"function_name": t_name, "timeout_seconds": cur_to * 2}
            rb_tool = "aws_update_lambda_timeout"
            rb_params = {"function_name": t_name, "timeout_seconds": cur_to}
            v_tool = "aws_get_function_configuration"
            return (
                format_plan(
                    tool, params, rb_tool, rb_params, v_tool, t_name, approval_reason
                ),
                history,
            )
        if prov == "kubernetes" and "k8s_restart_deployment" in prior_tools:
            tool = "k8s_rollback_deployment"
            params = {
                "namespace": t_ns,
                "resource_name": dep_name,
                "deployment_name": dep_name,
            }
            rb_tool = "k8s_rollback_deployment"
            rb_params = dict(params)
            v_tool = "k8s_describe_resource"
            return (
                format_plan(
                    tool, params, rb_tool, rb_params, v_tool, dep_name, approval_reason
                ),
                history,
            )

    if suggested and (
        (prov == "aws" and not suggested.startswith("k8s_"))
        or (prov == "kubernetes" and not suggested.startswith("aws_"))
        or (
            prov not in ("aws", "kubernetes")
            and not suggested.startswith(("k8s_", "aws_"))
        )
    ):
        tool = suggested
    elif (
        live_cfg.get("has_command_override")
        or live_cfg.get("exit_code") == 127
        or "command override" in root_cause
        or "startup script" in root_cause
    ):
        tool = "k8s_remove_container_command"
        params = {
            "namespace": t_ns,
            "resource_name": dep_name,
            "deployment_name": dep_name,
        }
        rb_tool = "k8s_restart_deployment"
        rb_params = dict(params)
        v_tool = "k8s_describe_resource"
        return format_plan(tool, params, rb_tool, rb_params, v_tool, dep_name), None
    elif (
        any(
            e.get("signal_type") == "traffic_spike"
            for e in events
            if isinstance(e, dict)
        )
        or fail_cls == "cascading_failure"
        or "traffic" in root_cause
    ):
        cur_rep = int(metrics.get("current_replicas") or live_cfg.get("replicas", 2))
        ratio = float(metrics.get("traffic_ratio", 2.0))
        target_rep = int(round(cur_rep * ratio))
        tool = "k8s_scale_resource"
        params = {
            "namespace": t_ns,
            "resource_name": dep_name,
            "deployment_name": dep_name,
            "replicas": target_rep,
        }
        rb_tool = "k8s_scale_resource"
        rb_params = {
            "namespace": t_ns,
            "resource_name": dep_name,
            "deployment_name": dep_name,
            "replicas": cur_rep,
        }
        v_tool = "k8s_describe_resource"
        return format_plan(tool, params, rb_tool, rb_params, v_tool, dep_name), None
    elif prov == "kubernetes" and (
        telem.get("health_status") == "oomkilled"
        or live_cfg.get("exit_code") == 137
        or "oom" in root_cause
        or "exit code 137" in root_cause
    ):
        cur_mem = str(live_cfg.get("memory_limit", "256Mi"))
        target_mem = double_memory_limit(cur_mem)
        tool = "k8s_patch_resource_limits"
        params = {
            "namespace": t_ns,
            "resource_name": dep_name,
            "deployment_name": dep_name,
            "memory_limit": target_mem,
        }
        rb_tool = "k8s_patch_resource_limits"
        rb_params = {
            "namespace": t_ns,
            "resource_name": dep_name,
            "deployment_name": dep_name,
            "memory_limit": cur_mem,
        }
        v_tool = "k8s_describe_resource"
        return format_plan(tool, params, rb_tool, rb_params, v_tool, dep_name), None
    elif prov == "aws":
        tool = "aws_update_lambda_memory"
    elif (
        fail_cls == "bad_deploy"
        or "revision" in root_cause
        or "rollback" in root_cause
        or "regression" in root_cause
    ):
        tool = "k8s_rollback_deployment"
    elif "deploy" in fail_cls or "deploy" in root_cause or "crash" in root_cause:
        tool = "k8s_restart_deployment"
    elif prov not in ("aws", "kubernetes"):
        from nexus.graph.platform import get_platform_registry

        ad = get_platform_registry().get(prov)
        tool = (
            ad.get_tools()[0].name
            if (ad and hasattr(ad, "get_tools") and ad.get_tools())
            else "k8s_restart_deployment"
        )
    else:
        tool = "k8s_restart_deployment"

    if tool == "aws_update_lambda_memory":
        params = diag.get("action_params") or {
            "function_name": t_name,
            "memory_mb": 512,
            "region": t_ns if t_ns != "default" else "us-east-1",
        }
        rb_tool, rb_params, v_tool = (
            tool,
            {
                "function_name": t_name,
                "memory_mb": 256,
                "region": t_ns if t_ns != "default" else "us-east-1",
            },
            "aws_get_function_configuration",
        )
    elif tool == "gcp_restart_compute_instance":
        params = diag.get("action_params") or {
            "project_id": "prod-proj",
            "zone": "us-central1-a",
            "instance_name": t_name,
        }
        rb_tool, rb_params, v_tool = tool, params, tool
    else:
        params = diag.get("action_params") or {
            "namespace": t_ns,
            "resource_name": dep_name,
            "deployment_name": dep_name,
        }
        rb_tool = (
            "k8s_rollback_deployment"
            if tool == "k8s_rollback_deployment"
            else "k8s_restart_deployment"
        )
        rb_params, v_tool = dict(params), "k8s_describe_resource"

    return (
        format_plan(
            tool, params, rb_tool, rb_params, v_tool, dep_name, approval_reason
        ),
        None,
    )


async def plan_node(
    state: dict[str, Any], llm: Any = None, registry: ToolRegistry | None = None
) -> dict[str, Any]:
    reg = registry or default_registry
    history = None
    plan_data: dict[str, Any] | None = None
    if state.get("fallback_active"):
        plan_data, history = _build_deterministic_plan(state)
    else:
        model = llm or get_llm()
        rca = state.get("rca_report") or state.get("diagnosis") or {}
        prompt = f"{PLANNER_PROMPT}\nRCA:\n{json.dumps(rca, default=str)}\nTools:\n{json.dumps(reg.get_mutating_tool_schemas())}"

        try:
            if hasattr(model, "with_structured_output"):
                try:
                    s_llm = model.with_structured_output(RemediationPlan)
                    call = (
                        s_llm.ainvoke(prompt)
                        if hasattr(s_llm, "ainvoke")
                        else s_llm.invoke(prompt)
                    )
                    res = await call if asyncio.iscoroutine(call) else call
                    if isinstance(res, RemediationPlan):
                        plan_data = res.model_dump()
                    elif isinstance(res, dict):
                        plan_data = res
                except NotImplementedError:
                    pass
            if plan_data is None:
                call = (
                    model.ainvoke(prompt)
                    if hasattr(model, "ainvoke")
                    else model.invoke(prompt)
                )
                resp = await call if asyncio.iscoroutine(call) else call
                raw = resp.content if hasattr(resp, "content") else str(resp)
                m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
                plan_data = json.loads(m.group(1) if m else raw)
        except Exception as exc:
            logger.warning(
                "[Plan] LLM planning failed: %s; using deterministic fallback", exc
            )
            plan_data, history = _build_deterministic_plan(state)

    if not plan_data:
        plan_data, history = _build_deterministic_plan(state)

    # Validate tool names against registry
    steps = plan_data.get("steps") or []
    actions = plan_data.get("actions") or []
    valid_tools = {s.get("name") for s in reg.get_mutating_tool_schemas()} | {
        "k8s_scale_resource",
        "k8s_restart_deployment",
        "k8s_rollback_deployment",
        "execute_aws_mutate",
        "execute_k8s_mutate",
        "gcp_restart_compute_instance",
        "k8s_remove_container_command",
        "k8s_patch_resource_limits",
        "aws_update_lambda_timeout",
    }
    for item in steps + actions:
        tn = item.get("tool_name", "")
        if tn and tn not in valid_tools and not reg.get(tn):
            logger.warning(
                "[Plan] Unregistered tool '%s' proposed; falling back to deterministic",
                tn,
            )
            plan_data, history = _build_deterministic_plan(state)
            break

    if "steps" not in plan_data and "actions" in plan_data:
        plan_data["steps"] = [
            {
                "step_index": a.get("step_number", i),
                "tool_name": a.get("tool_name"),
                "parameters": a.get("tool_arguments", {}),
                "rollback_tool": a.get("rollback_tool_name"),
                "rollback_parameters": a.get("rollback_arguments", {}),
                "action_category": a.get("action_category", "restart"),
                "blast_radius": a.get("blast_radius", "pod_or_instance"),
                "reversible": a.get("reversible", True),
            }
            for i, a in enumerate(plan_data["actions"])
        ]
    elif "actions" not in plan_data and "steps" in plan_data:
        plan_data["actions"] = [
            {
                "step_number": s.get("step_index", i),
                "tool_name": s.get("tool_name", ""),
                "tool_arguments": s.get("parameters", {}),
                "rollback_tool_name": s.get("rollback_tool", ""),
                "rollback_arguments": s.get("rollback_parameters", {}),
                "action_category": s.get("action_category", "restart"),
                "blast_radius": s.get("blast_radius", "pod_or_instance"),
                "reversible": s.get("reversible", True),
            }
            for i, s in enumerate(plan_data["steps"])
        ]

    now = datetime.now(timezone.utc).isoformat()
    audit = list(state.get("audit_log", [])) + [
        {
            "timestamp": now,
            "stage": "plan",
            "action": "plan_generated",
            "details": f"Generated plan with {len(plan_data.get('steps', []))} steps",
        }
    ]
    plan_data["requires_approval"] = True
    result_dict: dict[str, Any] = {
        "remediation_plan": plan_data,
        "plan": plan_data,
        "fsm_state": IncidentState.PLANNING.value,
        "audit_log": audit,
    }
    if history:
        result_dict["remediation_history"] = history
    return result_dict


def plan_remediation_node(state: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
    try:
        asyncio.get_running_loop()
        return plan_node(state, *args, **kwargs)
    except RuntimeError:
        return asyncio.run(plan_node(state, *args, **kwargs))
