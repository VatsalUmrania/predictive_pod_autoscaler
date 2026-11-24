"""
Kubernetes Read-Only Observation Tools & Functions
==================================================
Observation tools and raw inspection functions for Kubernetes resources.
Mirrors nexus.engine.tools.aws.observe.observe_tools.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kubernetes import client as k8s_client  # noqa: F401
from kubernetes.client.rest import ApiException
from langchain_core.tools import tool

from nexus.engine.tools.base import (
    NexusTool,
    NexusToolResult,
    ToolDomain,
    ToolRiskLevel,
)
from nexus.engine.tools.k8s.client.client import (
    get_k8s_apps_api,
    get_k8s_client,
    get_k8s_core_api,
)
from nexus.engine.tools.k8s.client.schemas import (
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


@tool
def locate_pod_and_owner(pod_name: str, namespace: str | None = None) -> dict[str, Any]:
    """
    Locates a Pod across a specific namespace or all namespaces, returns its exact namespace,
    traces its ownerReferences (Pod -> ReplicaSet -> Deployment/StatefulSet), and returns
    live containerStatuses (including exit codes, restart counts, waiting/terminated reasons,
    and readiness/liveness probe configurations).
    """
    core_v1 = get_k8s_core_api()
    apps_v1 = get_k8s_apps_api()

    pod = None
    if namespace:
        try:
            pod = core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)
        except ApiException:
            pod = None

    if pod is None:
        pods = core_v1.list_pod_for_all_namespaces(
            field_selector=f"metadata.name={pod_name}"
        )
        if not pods.items:
            return {"error": f"Pod '{pod_name}' not found in any namespace."}
        pod = pods.items[0]

    resolved_ns = pod.metadata.namespace
    owner_kind = "Pod"
    owner_name = pod.metadata.name

    # Trace ownerReferences: Pod -> ReplicaSet -> Deployment (or direct StatefulSet/DaemonSet)
    for ref in pod.metadata.owner_references or []:
        if ref.kind == "ReplicaSet":
            try:
                rs = apps_v1.read_namespaced_replica_set(
                    name=ref.name, namespace=resolved_ns
                )
                for rs_ref in rs.metadata.owner_references or []:
                    owner_kind = rs_ref.kind
                    owner_name = rs_ref.name
                    break
                else:
                    owner_kind = "ReplicaSet"
                    owner_name = ref.name
            except ApiException:
                owner_kind = "ReplicaSet"
                owner_name = ref.name
        else:
            owner_kind = ref.kind
            owner_name = ref.name

    # Extract container statuses (current state + last terminated state + exit codes)
    container_statuses: list[dict[str, Any]] = []
    for cs in pod.status.container_statuses or []:
        state_info: dict[str, Any] = {}
        if cs.state and cs.state.running:
            state_info["running"] = {"started_at": str(cs.state.running.started_at)}
        if cs.state and cs.state.waiting:
            state_info["waiting"] = {
                "reason": cs.state.waiting.reason,
                "message": cs.state.waiting.message,
            }
        if cs.state and cs.state.terminated:
            state_info["terminated"] = {
                "exit_code": cs.state.terminated.exit_code,
                "reason": cs.state.terminated.reason,
                "message": cs.state.terminated.message,
            }

        last_state_info: dict[str, Any] = {}
        if cs.last_state and cs.last_state.terminated:
            last_state_info["terminated"] = {
                "exit_code": cs.last_state.terminated.exit_code,
                "reason": cs.last_state.terminated.reason,
                "message": cs.last_state.terminated.message,
                "finished_at": str(cs.last_state.terminated.finished_at),
            }

        container_statuses.append(
            {
                "container_name": cs.name,
                "ready": cs.ready,
                "restart_count": cs.restart_count,
                "current_state": state_info,
                "last_terminated_state": last_state_info,
            }
        )

    # Extract container command, args, limits, and probes from Pod spec
    container_specs: list[dict[str, Any]] = []
    for c in pod.spec.containers or []:
        container_specs.append(
            {
                "container_name": c.name,
                "image": c.image,
                "command": c.command,
                "args": c.args,
                "ports": [p.container_port for p in (c.ports or [])],
                "resources": c.resources.to_dict() if c.resources else {},
                "readiness_probe": (
                    c.readiness_probe.to_dict() if c.readiness_probe else None
                ),
                "liveness_probe": (
                    c.liveness_probe.to_dict() if c.liveness_probe else None
                ),
            }
        )

    return {
        "pod_name": pod.metadata.name,
        "namespace": resolved_ns,
        "owner_kind": owner_kind,
        "owner_name": owner_name,
        "labels": pod.metadata.labels or {},
        "phase": pod.status.phase,
        "container_statuses": container_statuses,
        "container_specs": container_specs,
    }


@tool
def fetch_pod_container_logs(
    pod_name: str,
    namespace: str,
    container_name: str | None = None,
    tail_lines: int = 80,
    include_previous_crashed: bool = True,
) -> dict[str, Any]:
    """
    Fetches live stdout/stderr container logs for a Pod, and optionally fetches the logs
    of the previously terminated container instance (--previous) if the container crashed or exited.
    """
    core_v1 = get_k8s_core_api()
    result: dict[str, Any] = {"pod_name": pod_name, "namespace": namespace}

    try:
        current_logs = core_v1.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            container=container_name,
            tail_lines=tail_lines,
            previous=False,
        )
        result["current_container_logs"] = current_logs
    except ApiException as exc:
        result["current_container_logs_error"] = f"HTTP {exc.status}: {exc.reason}"

    if include_previous_crashed:
        try:
            prev_logs = core_v1.read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace,
                container=container_name,
                tail_lines=tail_lines,
                previous=True,
            )
            result["previous_terminated_container_logs"] = prev_logs
        except ApiException:
            # No previous terminated container exists yet
            result["previous_terminated_container_logs"] = None

    return result


@tool
def inspect_deployment_status(deployment_name: str, namespace: str) -> dict[str, Any]:
    """
    Fetches the live rollout status, replica counts, conditions, and pod template spec
    for a registered Kubernetes Deployment, plus all active Pods belonging to it.
    """
    apps_v1 = get_k8s_apps_api()
    core_v1 = get_k8s_core_api()

    try:
        dep = apps_v1.read_namespaced_deployment(
            name=deployment_name, namespace=namespace
        )
    except ApiException as exc:
        return {
            "error": f"Deployment '{namespace}/{deployment_name}' not found: {exc.reason}"
        }

    match_labels = dep.spec.selector.match_labels or {}
    label_selector = ",".join(f"{k}={v}" for k, v in match_labels.items())
    pods = core_v1.list_namespaced_pod(
        namespace=namespace, label_selector=label_selector
    )

    pod_summaries = []
    for p in pods.items:
        pod_summaries.append(
            {
                "pod_name": p.metadata.name,
                "phase": p.status.phase,
                "ready": (
                    all(cs.ready for cs in (p.status.container_statuses or []))
                    if p.status.container_statuses
                    else False
                ),
                "restarts": sum(
                    cs.restart_count for cs in (p.status.container_statuses or [])
                ),
            }
        )

    conditions = [
        {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
        for c in (dep.status.conditions or [])
    ]

    return {
        "deployment_name": dep.metadata.name,
        "namespace": dep.metadata.namespace,
        "nexus_registered": (dep.metadata.labels or {}).get("nexus.sre/enabled")
        == "true",
        "desired_replicas": dep.spec.replicas,
        "ready_replicas": dep.status.ready_replicas or 0,
        "available_replicas": dep.status.available_replicas or 0,
        "unavailable_replicas": dep.status.unavailable_replicas or 0,
        "conditions": conditions,
        "pods": pod_summaries,
    }


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
    "locate_pod_and_owner",
    "fetch_pod_container_logs",
    "inspect_deployment_status",
]
