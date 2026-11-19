"""
NEXUS Manifest & Diagnostic Log Formatter
=========================================
Synthesizes clean Kubernetes/AWS YAML remediation manifests and extracts
verifiable diagnostic logs from incident traces for Slack and PostgreSQL.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import yaml

__all__ = ["plan_to_yaml_manifest", "extract_diagnostic_logs"]


def _format_k8s_action(
    tool: str, params: dict[str, Any], target: str
) -> dict[str, Any]:
    ns = params.get("namespace") or (
        target.split("/")[0] if "/" in target else "default"
    )
    name = (
        params.get("resource_name")
        or params.get("deployment_name")
        or params.get("name")
        or (target.split("/")[1] if "/" in target else target or "workload")
    )
    kind = params.get("kind", "Deployment")
    api_version = params.get("api_version", "apps/v1")

    base = {
        "apiVersion": api_version,
        "kind": kind,
        "metadata": {"name": name, "namespace": ns},
    }

    if tool in ("scale_replicas", "k8s_scale_resource") or "replicas" in params:
        replicas = int(params.get("replicas", 1))
        base["spec"] = {"replicas": replicas}
        return base

    if (
        tool in ("k8s_patch_resource_limits",)
        or "memory_limit" in params
        or "cpu_limit" in params
    ):
        limits: dict[str, str] = {}
        if params.get("memory_limit"):
            limits["memory"] = str(params["memory_limit"])
        if params.get("cpu_limit"):
            limits["cpu"] = str(params["cpu_limit"])
        base["spec"] = {
            "template": {
                "spec": {
                    "containers": [{"name": name, "resources": {"limits": limits}}]
                }
            }
        }
        return base

    if tool in ("rollout_restart", "k8s_restart_deployment"):
        now_ts = datetime.now(timezone.utc).isoformat()
        base["spec"] = {
            "template": {
                "metadata": {
                    "annotations": {"kubectl.kubernetes.io/restartedAt": now_ts}
                }
            }
        }
        return base

    if "patch_body" in params and isinstance(params["patch_body"], dict):
        base.update(params["patch_body"])
        return base

    base["spec"] = {"parameters": params}
    return base


def plan_to_yaml_manifest(plan: dict[str, Any] | None, target: str = "") -> str:
    """Format planned remediation actions as a valid YAML manifest or action spec."""
    if not plan:
        return "# No active remediation plan\n"

    actions = plan.get("actions") or plan.get("steps") or []
    if not actions:
        summary = plan.get("summary") or "Remediation plan"
        return str(
            yaml.safe_dump(
                {"summary": summary, "status": "no_mutations_needed"}, sort_keys=False
            )
        )

    rendered_docs: list[dict[str, Any]] = []
    for act in actions:
        tool = str(act.get("tool_name") or act.get("action") or act.get("tool") or "")
        params = (
            act.get("tool_arguments")
            or act.get("parameters")
            or act.get("arguments")
            or {}
        )
        act_target = act.get("target_resource_urn") or act.get("target") or target

        if (
            tool.startswith(("k8s_", "kubernetes_"))
            or "namespace" in params
            or act.get("mutation_type")
            or "/" in act_target
        ):
            rendered_docs.append(_format_k8s_action(tool, params, act_target))
        else:
            rendered_docs.append(
                {
                    "action": tool,
                    "target": act_target,
                    "parameters": params,
                    "rollback": {
                        "tool": act.get("rollback_tool_name")
                        or act.get("rollback_tool"),
                        "parameters": act.get("rollback_arguments")
                        or act.get("rollback_parameters", {}),
                    },
                }
            )

    if len(rendered_docs) == 1:
        return str(yaml.safe_dump(rendered_docs[0], sort_keys=False))
    return str(yaml.safe_dump_all(rendered_docs, sort_keys=False))


def extract_diagnostic_logs(state_or_rca: dict[str, Any], max_lines: int = 25) -> str:
    """Extract verifiable pod logs and diagnostic evidence traces that identified the failure."""
    lines: list[str] = []

    # 1. From evidence_chain in rca_report
    rca = (
        state_or_rca.get("rca_report")
        or state_or_rca.get("rca")
        or state_or_rca.get("diagnosis")
        or state_or_rca
    )
    for ev in rca.get("evidence_chain", []):
        if isinstance(ev, dict) and ev.get("key_finding"):
            lines.append(f"[{ev.get('tool_name', 'evidence')}] {ev['key_finding']}")

    # 2. From investigation_trace
    trace = state_or_rca.get("investigation_trace") or []
    for item in trace:
        if isinstance(item, dict):
            tn = item.get("tool_name", "")
            out = item.get("output", "")
            if tn == "k8s_get_logs" and out:
                raw_lines = str(out).strip().splitlines()
                lines.extend(raw_lines[-15:])
            elif tn in ("k8s_inspect_resource", "aws_query_telemetry") and out:
                lines.append(f"[{tn}] {str(out)[:300]}")

    # 3. From raw events / telemetry if trace is empty
    if not lines:
        telem = state_or_rca.get("telemetry") or {}
        if telem.get("logs"):
            raw_logs = str(telem["logs"]).strip().splitlines()
            lines.extend(raw_logs[-15:])
        elif telem.get("health_status"):
            lines.append(f"Pod health status: {telem['health_status']}")

    if not lines:
        for ev in state_or_rca.get("events", []):
            if isinstance(ev, dict) and ev.get("signal_type"):
                lines.append(
                    f"Signal: {ev.get('signal_type')} ({ev.get('severity', 'warning')}) - {json.dumps(ev.get('context', {}))}"
                )

    if not lines:
        root_cause = rca.get("root_cause") or rca.get("root_cause_summary")
        if root_cause:
            lines.append(f"Diagnosed cause: {root_cause}")
        else:
            lines.append("No container log snippet captured during investigation.")

    selected = lines[-max_lines:]
    return "\n".join(selected)[:3000]
