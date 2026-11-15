"""Main dispatch for live Kubernetes and cloud state validation."""

from __future__ import annotations

import logging
from typing import Any

from nexus.governance.live_state.models import (
    _ALWAYS_PROCEED,
    _ALWAYS_PROCEED_UNVERIFIABLE,
    LiveStateReport,
    proceed_always,
    proceed_unverifiable,
    unknown_report,
)

logger = logging.getLogger(__name__)


async def check_live_state(
    action_type: str,
    namespace: str,
    resource_name: str,
    k8s_core: Any = None,
    k8s_apps: Any = None,
) -> LiveStateReport:
    """Re-query provider to confirm failure condition is still active."""
    if action_type in _ALWAYS_PROCEED:
        return proceed_always(action_type)

    if action_type in _ALWAYS_PROCEED_UNVERIFIABLE:
        return proceed_unverifiable(action_type)

    if action_type not in (
        "restart_pod",
        "restart_deployment",
        "k8s_restart_deployment",
        "kubectl_rollout_undo",
        "rollback_deployment",
        "k8s_rollback_deployment",
        "scale_deployment",
        "scale_resource",
        "k8s_scale_deployment",
    ):
        return LiveStateReport(
            condition_still_active=True,
            verdict="proceed",
            evidence={"reason": f"no live check defined for action {action_type!r}"},
            action_type=action_type,
        )

    try:
        if action_type in (
            "restart_pod",
            "restart_deployment",
            "k8s_restart_deployment",
        ):
            return _check_pod_condition(action_type, namespace, resource_name, k8s_core)

        return _check_deployment_condition(
            action_type, namespace, resource_name, k8s_apps
        )
    except Exception as exc:
        return unknown_report(action_type, exc)


def _check_pod_condition(
    action_type: str, namespace: str, resource_name: str, k8s_core: Any
) -> LiveStateReport:
    pods = k8s_core.list_namespaced_pod(namespace=namespace).items
    matching = [
        p
        for p in pods
        if any(
            getattr(r, "kind", "") == "ReplicaSet"
            and getattr(r, "name", "").startswith(resource_name)
            for r in (getattr(p.metadata, "owner_references", []) or [])
        )
        or getattr(p.metadata, "name", "").startswith(resource_name)
    ]
    if not matching:
        return LiveStateReport(
            condition_still_active=True,
            verdict="proceed",
            evidence={"reason": "no pods found"},
            action_type=action_type,
        )

    failing_pods = []
    for pod in matching:
        st = getattr(pod, "status", None)
        phase = getattr(st, "phase", "")
        for cs in getattr(st, "container_statuses", []) or []:
            wait = getattr(getattr(cs, "state", None), "waiting", None)
            w_reason = getattr(wait, "reason", "") if wait else ""
            term = getattr(getattr(cs, "last_state", None), "terminated", None)
            t_reason = getattr(term, "reason", "") if term else ""
            restarts = getattr(cs, "restart_count", 0) or 0
            ready = getattr(cs, "ready", False)
            if (
                w_reason in ("CrashLoopBackOff", "Error", "CreateContainerConfigError")
                or t_reason in ("OOMKilled", "Error")
                or restarts >= 3
                or (restarts >= 1 and not ready)
                or phase in ("Pending", "Failed")
            ):
                failing_pods.append(
                    {
                        "pod": pod.metadata.name,
                        "failure_reason": w_reason or t_reason or phase or "restarts",
                        "restarts": restarts,
                    }
                )
                break

    if failing_pods:
        return LiveStateReport(
            condition_still_active=True,
            verdict="proceed",
            evidence={"failing_pods": failing_pods},
            action_type=action_type,
        )

    return LiveStateReport(
        condition_still_active=False,
        verdict="self_healed",
        evidence={"all_running_ready": True, "count": len(matching)},
        action_type=action_type,
    )


def _check_deployment_condition(
    action_type: str, namespace: str, resource_name: str, k8s_apps: Any
) -> LiveStateReport:
    dep = k8s_apps.read_namespaced_deployment(name=resource_name, namespace=namespace)
    desired = getattr(dep.spec, "replicas", 1) or 1
    available = getattr(dep.status, "available_replicas", 0) or 0
    if available < desired:
        return LiveStateReport(
            condition_still_active=True,
            verdict="proceed",
            evidence={"available_replicas": available, "desired_replicas": desired},
            action_type=action_type,
        )
    return LiveStateReport(
        condition_still_active=False,
        verdict="self_healed",
        evidence={
            "all_replicas_available": True,
            "available_replicas": available,
            "desired_replicas": desired,
        },
        action_type=action_type,
    )
