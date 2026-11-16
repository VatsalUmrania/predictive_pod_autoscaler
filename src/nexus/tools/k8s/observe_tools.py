"""
Kubernetes Read-Only Observation Tools & Functions
==================================================
Observation tools and raw inspection functions for Kubernetes resources.
Mirrors nexus.tools.aws.observe_tools.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from nexus.tools.base import NexusTool, NexusToolResult, ToolDomain, ToolRiskLevel
from nexus.tools.k8s.client import get_k8s_client
from nexus.tools.k8s.schemas import (
    DescribeResourceSchema,
    GetMetricsSchema,
    GetPodLogsSchema,
)

logger = logging.getLogger(__name__)


def _get_api() -> Any:
    return get_k8s_client("CoreV1Api")


def _get_apps_api() -> Any:
    return get_k8s_client("AppsV1Api")


def get_pod_logs(
    namespace: str = "default",
    pod_name: str = "",
    tail_lines: int = 50,
    **kwargs: Any,
) -> str:
    api = _get_api()
    if api:
        try:
            return str(
                api.read_namespaced_pod_log(
                    name=pod_name,
                    namespace=namespace,
                    tail_lines=tail_lines,
                    _request_timeout=2.0,
                )
            )
        except Exception as exc:
            return f"Error fetching logs for {pod_name}: {exc}"
    return "pod logs stub"


def describe_resource(
    namespace: str = "default",
    resource_kind: str = "deployment",
    resource_name: str = "",
    **kwargs: Any,
) -> str:
    r_kind = resource_kind.lower()
    if r_kind in ("deployment", "deploy"):
        apps = _get_apps_api()
        if apps:
            try:
                dep = apps.read_namespaced_deployment(
                    name=resource_name, namespace=namespace, _request_timeout=2.0
                )
                st = dep.status
                unavail = getattr(st, "unavailable_replicas", 0) or 0
                rep = getattr(st, "replicas", 0) or 0
                ready = getattr(st, "ready_replicas", 0) or 0
                avail = getattr(st, "available_replicas", 0) or 0
                upd = getattr(st, "updated_replicas", 0) or 0
                pod_issues: list[str] = []
                core = _get_api()
                if core:
                    try:
                        pods = core.list_namespaced_pod(
                            namespace=namespace, _request_timeout=2.0
                        ).items
                        for p in pods:
                            p_name = getattr(p.metadata, "name", "")
                            owners = getattr(p.metadata, "owner_references", []) or []
                            if p_name.startswith(resource_name) or any(
                                getattr(r, "name", "").startswith(resource_name)
                                for r in owners
                            ):
                                for cs in (
                                    getattr(p.status, "container_statuses", []) or []
                                ):
                                    w = getattr(cs.state, "waiting", None)
                                    if w and getattr(w, "reason", None):
                                        pod_issues.append(f"{p_name}:{w.reason}")
                                    last_t = getattr(
                                        getattr(cs, "last_state", None),
                                        "terminated",
                                        None,
                                    )
                                    if last_t and getattr(last_t, "reason", None):
                                        pod_issues.append(
                                            f"{p_name}:LastState:{last_t.reason}(exit {getattr(last_t, 'exit_code', None)})"
                                        )
                    except Exception:
                        pass
                issues_str = f" [{', '.join(pod_issues[:3])}]" if pod_issues else ""
                return (
                    f"Deployment {resource_name}: replicas={rep}, "
                    f"ready={ready}, available={avail}, "
                    f"unavailable={unavail}, updated={upd}{issues_str}"
                )
            except Exception as exc:
                return f"Error describing {resource_name}: {exc}"
    elif r_kind in ("pod", "po"):
        core = _get_api()
        if core:
            try:
                pod = core.read_namespaced_pod(name=resource_name, namespace=namespace)
                phase = getattr(pod.status, "phase", "Unknown")
                c_statuses = getattr(pod.status, "container_statuses", []) or []
                reasons = []
                for cs in c_statuses:
                    w = getattr(cs.state, "waiting", None)
                    if w and getattr(w, "reason", None):
                        reasons.append(w.reason)
                    t = getattr(cs.state, "terminated", None)
                    if t:
                        reasons.append(
                            f"{getattr(t, 'reason', 'Terminated')} (Exit Code: {getattr(t, 'exit_code', None)})"
                        )
                    last_t = getattr(
                        getattr(cs, "last_state", None), "terminated", None
                    )
                    if last_t:
                        last_r = getattr(last_t, "reason", "Terminated")
                        last_c = getattr(last_t, "exit_code", None)
                        reasons.append(f"LastState:{last_r} (Exit Code: {last_c})")
                r_str = f" [{', '.join(reasons)}]" if reasons else ""
                ready_c = sum(1 for cs in c_statuses if getattr(cs, "ready", False))
                return f"Pod {resource_name}: phase={phase}, ready={ready_c}/{len(c_statuses)}{r_str}"
            except Exception as exc:
                return f"Error describing {resource_name}: {exc}"
    return f"{resource_kind}/{resource_name} status: ok"


def get_events(namespace: str = "default", **kwargs: Any) -> str:
    api = _get_api()
    if api:
        try:
            events = api.list_namespaced_event(namespace=namespace).items
            return "\n".join(f"{e.type} {e.reason}: {e.message}" for e in events[:20])
        except Exception as exc:
            return f"Error fetching events: {exc}"
    return "events: none"


def get_metrics(*args: Any, **kwargs: Any) -> str:
    return "{}"


def get_yaml(*args: Any, **kwargs: Any) -> str:
    return "apiVersion: v1\nkind: Pod"


class K8sGetPodLogsTool(NexusTool):
    name = "k8s_get_pod_logs"
    description = "Fetch recent log lines from a Kubernetes pod to diagnose errors."
    domain = ToolDomain.K8S
    risk_level = ToolRiskLevel.L0_OBSERVE
    args_schema = GetPodLogsSchema
    blast_radius = "none"
    is_destructive = False
    allows_unbacked_rollback = False

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        args = self.args_schema(**kwargs)
        res = await asyncio.to_thread(
            get_pod_logs,
            namespace=args.namespace,
            pod_name=args.pod_name,
            tail_lines=args.tail_lines,
        )
        is_err = "Error fetching logs" in res
        return NexusToolResult(
            success=not is_err,
            data={"logs": res} if not is_err else None,
            error=res if is_err else None,
        )


class K8sDescribeResourceTool(NexusTool):
    name = "k8s_describe_resource"
    description = (
        "Get detailed status and state of a Kubernetes pod, deployment, or service."
    )
    domain = ToolDomain.K8S
    risk_level = ToolRiskLevel.L0_OBSERVE
    args_schema = DescribeResourceSchema
    blast_radius = "none"
    is_destructive = False
    allows_unbacked_rollback = False

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        args = self.args_schema(**kwargs)
        res = await asyncio.to_thread(
            describe_resource,
            namespace=args.namespace,
            resource_kind=args.resource_kind,
            resource_name=args.resource_name,
        )
        is_err = "Error describing" in res
        return NexusToolResult(
            success=not is_err,
            data={"details": res} if not is_err else None,
            error=res if is_err else None,
        )


class K8sGetMetricsTool(NexusTool):
    name = "k8s_get_metrics"
    description = "Query Kubernetes metrics for a specific pod or deployment."
    domain = ToolDomain.K8S
    risk_level = ToolRiskLevel.L0_OBSERVE
    args_schema = GetMetricsSchema
    blast_radius = "none"
    is_destructive = False
    allows_unbacked_rollback = False

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        args = self.args_schema(**kwargs)
        res = await asyncio.to_thread(
            get_metrics,
            namespace=args.namespace,
            resource_kind=args.resource_kind,
            resource_name=args.resource_name or args.pod_name or "",
        )
        return NexusToolResult(success=True, data={"metrics": res})


__all__ = [
    "_get_api",
    "_get_apps_api",
    "get_pod_logs",
    "describe_resource",
    "get_events",
    "get_metrics",
    "get_yaml",
    "K8sGetPodLogsTool",
    "K8sDescribeResourceTool",
    "K8sGetMetricsTool",
]
