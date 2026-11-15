"""
NEXUS Plan Helpers
==================
Deterministic plan generation and adaptive recovery ladder escalation.
"""

from __future__ import annotations

import re
from typing import Any


def double_memory_limit(cur: str) -> str:
    m = re.match(r"^(\d+)([A-Za-z]+)$", cur.strip())
    if m:
        return f"{int(m.group(1)) * 2}{m.group(2)}"
    return "512Mi"


def build_scenario_plan(
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

    # 1. Adaptive recovery escalation
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
                _format_plan(
                    tool, params, rb_tool, rb_params, v_tool, t_name, approval_reason
                ),
                history,
            )
        if prov == "kubernetes" and "k8s_restart_deployment" in prior_tools:
            tool = "k8s_rollback_deployment"
            params = {
                "namespace": t_ns,
                "resource_name": t_name,
                "deployment_name": t_name,
            }
            rb_tool = "k8s_rollback_deployment"
            rb_params = dict(params)
            v_tool = "k8s_describe_resource"
            return (
                _format_plan(
                    tool, params, rb_tool, rb_params, v_tool, t_name, approval_reason
                ),
                history,
            )

    # 2. Dynamic K8s command override failure
    if (
        live_cfg.get("has_command_override")
        or live_cfg.get("exit_code") == 127
        or "command override" in root_cause
        or "startup script" in root_cause
    ):
        tool = "k8s_remove_container_command"
        params = {"namespace": t_ns, "resource_name": t_name, "deployment_name": t_name}
        rb_tool = "k8s_restart_deployment"
        rb_params = dict(params)
        v_tool = "k8s_describe_resource"
        return _format_plan(tool, params, rb_tool, rb_params, v_tool, t_name), None

    # 3. Dynamic K8s scaling from traffic spike
    has_traffic = any(
        e.get("signal_type") == "traffic_spike" for e in events if isinstance(e, dict)
    )
    if has_traffic or fail_cls == "cascading_failure" or "traffic" in root_cause:
        cur_rep = int(metrics.get("current_replicas") or live_cfg.get("replicas", 2))
        ratio = float(metrics.get("traffic_ratio", 2.0))
        target_rep = int(round(cur_rep * ratio))
        tool = "k8s_scale_resource"
        params = {
            "namespace": t_ns,
            "resource_name": t_name,
            "deployment_name": t_name,
            "replicas": target_rep,
        }
        rb_tool = "k8s_scale_resource"
        rb_params = {
            "namespace": t_ns,
            "resource_name": t_name,
            "deployment_name": t_name,
            "replicas": cur_rep,
        }
        v_tool = "k8s_describe_resource"
        return _format_plan(tool, params, rb_tool, rb_params, v_tool, t_name), None

    # 4. Dynamic K8s patch limits from OOM
    if prov == "kubernetes" and (
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
            "resource_name": t_name,
            "deployment_name": t_name,
            "memory_limit": target_mem,
        }
        rb_tool = "k8s_patch_resource_limits"
        rb_params = {
            "namespace": t_ns,
            "resource_name": t_name,
            "deployment_name": t_name,
            "memory_limit": cur_mem,
        }
        v_tool = "k8s_describe_resource"
        return _format_plan(tool, params, rb_tool, rb_params, v_tool, t_name), None

    # 5. Base fallback
    if suggested and (
        (prov == "aws" and not suggested.startswith("k8s_"))
        or (prov == "kubernetes" and not suggested.startswith("aws_"))
        or (
            prov not in ("aws", "kubernetes")
            and not suggested.startswith(("k8s_", "aws_"))
        )
    ):
        tool = suggested
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
            "resource_name": t_name,
            "deployment_name": t_name,
        }
        rb_tool = (
            "k8s_rollback_deployment"
            if tool == "k8s_rollback_deployment"
            else "k8s_restart_deployment"
        )
        rb_params, v_tool = dict(params), "k8s_describe_resource"

    return (
        _format_plan(tool, params, rb_tool, rb_params, v_tool, t_name, approval_reason),
        None,
    )


def _format_plan(
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
