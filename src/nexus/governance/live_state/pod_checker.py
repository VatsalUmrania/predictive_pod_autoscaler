"""Pod failure state checker for live state validation."""
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


async def check_pod_failure_condition(
    action_type: str,
    namespace: str,
    resource_name: str,
    k8s_core: Any,
    k8s_apps: Any,
) -> LiveStateReport:
    """For restart_pod / restart_deployment: check if pods are failing or healthy."""
    loop = asyncio.get_running_loop()

    try:
        pod_list = await loop.run_in_executor(
            None,
            lambda: k8s_core.list_namespaced_pod(namespace),
        )
    except Exception as exc:
        logger.warning(f"[LiveStateValidator] Pod list failed for {namespace}/{resource_name}: {exc}")
        return unknown_report(action_type, exc)

    owned_pods = [p for p in pod_list.items if pod_belongs_to_deployment(p, resource_name)]
    if not owned_pods:
        logger.info(f"[LiveStateValidator] No pods found for deployment {namespace}/{resource_name}")
        return LiveStateReport(
            condition_still_active=True,
            verdict="proceed",
            evidence={"reason": f"no pods found for {namespace}/{resource_name}"},
            action_type=action_type,
        )

    failing_pods: list[dict[str, Any]] = []
    healthy_pods = 0

    for pod in owned_pods:
        phase = (getattr(pod.status, "phase", "") or "").lower()
        pod_info: dict[str, Any] = {
            "name": pod.metadata.name,
            "phase": phase,
            "ready": False,
            "restart_count": 0,
            "failure_reason": None,
        }

        for cs in getattr(pod.status, "container_statuses", None) or []:
            pod_info["restart_count"] = max(pod_info["restart_count"], cs.restart_count or 0)
            if cs.state and cs.state.waiting:
                reason = cs.state.waiting.reason or ""
                if reason == "CrashLoopBackOff":
                    pod_info["failure_reason"] = "CrashLoopBackOff"
            if cs.last_state and cs.last_state.terminated:
                if cs.last_state.terminated.reason == "OOMKilled":
                    pod_info["failure_reason"] = "OOMKilled"
            if cs.ready:
                pod_info["ready"] = True

        if phase == "pending":
            pod_info["failure_reason"] = "Pending"

        if pod_info["failure_reason"] or pod_info["restart_count"] >= 3:
            failing_pods.append(pod_info)
        elif phase == "running" and pod_info["ready"] and pod_info["restart_count"] == 0:
            healthy_pods += 1

    if failing_pods:
        logger.info(f"[LiveStateValidator] Condition confirmed for {namespace}/{resource_name}: {len(failing_pods)} failing pod(s)")
        return LiveStateReport(
            condition_still_active=True,
            verdict="proceed",
            evidence={"failing_pods": failing_pods, "healthy_pods": healthy_pods, "total_checked": len(owned_pods)},
            action_type=action_type,
        )

    logger.info(f"[LiveStateValidator] Condition RESOLVED for {namespace}/{resource_name}: self_healed")
    return LiveStateReport(
        condition_still_active=False,
        verdict="self_healed",
        evidence={"healthy_pods": healthy_pods, "total_checked": len(owned_pods), "all_running_ready": True},
        action_type=action_type,
    )
