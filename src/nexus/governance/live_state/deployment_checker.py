"""Deployment and scale condition checkers for live state validation."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from nexus.governance.live_state.models import (
    LiveStateReport,
    pod_belongs_to_deployment,
    unknown_report,
)

logger = logging.getLogger(__name__)


async def check_deployment_degraded(
    action_type: str,
    namespace: str,
    resource_name: str,
    k8s_apps: Any,
    k8s_core: Any = None,
) -> LiveStateReport:
    """Check if deployment is degraded or currently in failing rollout."""
    loop = asyncio.get_running_loop()

    try:
        dep = await loop.run_in_executor(
            None,
            lambda: k8s_apps.read_namespaced_deployment(resource_name, namespace),
        )
    except Exception as exc:
        logger.warning(
            f"[LiveStateValidator] Deployment read failed for {namespace}/{resource_name}: {exc}"
        )
        return unknown_report(action_type, exc)

    desired = (
        int(dep.spec.replicas)
        if getattr(dep, "spec", None)
        and getattr(dep.spec, "replicas", None) is not None
        else 1
    )
    available = (
        int(dep.status.available_replicas)
        if getattr(dep, "status", None)
        and getattr(dep.status, "available_replicas", None) is not None
        else 0
    )
    ready = (
        int(dep.status.ready_replicas)
        if getattr(dep, "status", None)
        and getattr(dep.status, "ready_replicas", None) is not None
        else 0
    )
    raw_unavail = (
        getattr(dep.status, "unavailable_replicas", 0)
        if getattr(dep, "status", None)
        else 0
    )
    unavailable = int(raw_unavail) if raw_unavail is not None else 0

    evidence = {
        "desired_replicas": desired,
        "available_replicas": available,
        "ready_replicas": ready,
        "unavailable_replicas": unavailable,
    }

    if available < desired or unavailable > 0:
        return LiveStateReport(
            condition_still_active=True,
            verdict="proceed",
            evidence=evidence,
            action_type=action_type,
        )

    if k8s_core is not None:
        try:
            pod_list = await loop.run_in_executor(
                None, lambda: k8s_core.list_namespaced_pod(namespace)
            )
            owned_pods = [
                p for p in pod_list.items if pod_belongs_to_deployment(p, resource_name)
            ]
            failing_pods: list[dict[str, Any]] = []
            for pod in owned_pods:
                phase = (getattr(pod.status, "phase", "") or "").lower()
                for cs in getattr(pod.status, "container_statuses", None) or []:
                    reason = (
                        cs.state.waiting.reason
                        if (cs.state and cs.state.waiting)
                        else None
                    )
                    term_state = (
                        cs.state.terminated
                        if (cs.state and cs.state.terminated)
                        else (cs.last_state.terminated if cs.last_state else None)
                    )
                    term_reason = term_state.reason if term_state else None
                    exit_code = term_state.exit_code if term_state else None
                    is_failing = (
                        reason
                        in ("CrashLoopBackOff", "Error", "CreateContainerConfigError")
                        or term_reason in ("OOMKilled", "Error")
                        or (exit_code is not None and exit_code != 0)
                        or (cs.restart_count and cs.restart_count >= 1 and not cs.ready)
                        or phase in ("pending", "failed")
                    )
                    if is_failing:
                        failing_pods.append(
                            {
                                "pod": pod.metadata.name,
                                "phase": phase,
                                "reason": reason or term_reason or f"exit_{exit_code}",
                                "restarts": cs.restart_count,
                            }
                        )
                        break
            if failing_pods:
                return LiveStateReport(
                    condition_still_active=True,
                    verdict="proceed",
                    evidence={**evidence, "failing_pods": failing_pods},
                    action_type=action_type,
                )
        except Exception as p_exc:
            logger.debug(
                f"[LiveStateValidator] Could not inspect pods for {resource_name}: {p_exc}"
            )

    return LiveStateReport(
        condition_still_active=False,
        verdict="self_healed",
        evidence={**evidence, "all_replicas_available": True},
        action_type=action_type,
    )


async def check_scale_condition(
    action_type: str,
    namespace: str,
    resource_name: str,
    k8s_apps: Any,
) -> LiveStateReport:
    """For scale_deployment: check if availableReplicas < spec.replicas."""
    loop = asyncio.get_running_loop()
    try:
        dep = await loop.run_in_executor(
            None, lambda: k8s_apps.read_namespaced_deployment(resource_name, namespace)
        )
    except Exception as exc:
        return unknown_report(action_type, exc)

    desired = dep.spec.replicas or 1
    available = dep.status.available_replicas or 0
    if available < desired:
        return LiveStateReport(
            condition_still_active=True,
            verdict="proceed",
            evidence={"desired_replicas": desired, "available_replicas": available},
            action_type=action_type,
        )

    return LiveStateReport(
        condition_still_active=False,
        verdict="self_healed",
        evidence={
            "desired_replicas": desired,
            "available_replicas": available,
            "all_replicas_available": True,
        },
        action_type=action_type,
    )
