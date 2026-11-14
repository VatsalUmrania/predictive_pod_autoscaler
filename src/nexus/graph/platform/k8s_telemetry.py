"""
Kubernetes telemetry collection, live state inspection, and health verification.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from nexus.graph.platform.base import (
    HealthCheckResult,
    LiveStateSnapshot,
    PlatformTelemetry,
    TargetResource,
)

logger = logging.getLogger(__name__)


def _parse_k8s_describe(desc: str, live_config: dict[str, Any]) -> None:
    """Extract structured runtime parameters from Kubernetes describe text."""
    import re

    m_rep = re.search(r"Replicas:\s*(\d+)", desc)
    if m_rep:
        live_config["replicas"] = int(m_rep.group(1))

    m_mem = re.search(
        r"(?:Limits:.*?|Requests:.*?)?memory:\s*([0-9]+(?:Mi|Gi|Ki|M|G|K))",
        desc,
        re.DOTALL | re.IGNORECASE,
    )
    if m_mem:
        live_config["memory_limit"] = m_mem.group(1)

    m_cpu = re.search(
        r"(?:Limits:.*?|Requests:.*?)?cpu:\s*([0-9]+m|[0-9.]+)",
        desc,
        re.DOTALL | re.IGNORECASE,
    )
    if m_cpu:
        live_config["cpu_limit"] = m_cpu.group(1)

    m_exit = re.search(
        r"(?:Exit Code:\s*|\(exit code\s*|exit code\s*)(\d+)", desc, re.IGNORECASE
    )
    if m_exit:
        live_config["exit_code"] = int(m_exit.group(1))

    m_container = re.search(r"Containers:\s*\n\s*([a-zA-Z0-9_\-]+):", desc)
    if m_container:
        live_config["container_name"] = m_container.group(1)

    if (
        re.search(r"Command:\s*\n\s+-[^\n]+", desc)
        or "exec format error" in desc.lower()
    ):
        live_config["has_command_override"] = True


async def collect_k8s_telemetry(target: TargetResource) -> PlatformTelemetry:
    """Deterministically collect logs, describe info, and live config."""
    from nexus.agents import k8s_tools

    recent_logs: list[str] = []
    live_config: dict[str, Any] = {}
    health_status = "degraded"

    try:
        desc_res = await asyncio.to_thread(
            k8s_tools.describe_resource,
            namespace=target.namespace,
            resource_kind=target.kind,
            resource_name=target.name,
        )
        live_config["describe"] = desc_res
        _parse_k8s_describe(desc_res, live_config)

        pod_for_logs = target.labels.get("pod_name") or target.name
        log_res = await asyncio.to_thread(
            k8s_tools.get_pod_logs,
            namespace=target.namespace,
            pod_name=pod_for_logs,
            tail_lines=50,
        )
        if "Error fetching logs" not in log_res:
            recent_logs.extend(log_res.splitlines()[-30:])
        else:
            recent_logs.append(log_res)

        lower_desc = desc_res.lower()
        if "crashloopbackoff" in lower_desc:
            health_status = "crashloopbackoff"
        elif "oomkilled" in lower_desc:
            health_status = "oomkilled"
        elif "pending" in lower_desc:
            health_status = "pending"
        elif "running" in lower_desc:
            health_status = "running"
    except Exception as exc:
        logger.warning(
            "[K8sPlatformAdapter] Telemetry gathering error for %s: %s", target, exc
        )
        live_config["error"] = str(exc)

    metrics: dict[str, Any] = {"health": health_status}
    if "replicas" in live_config:
        metrics["current_replicas"] = live_config["replicas"]
    if "memory_limit" in live_config:
        metrics["memory_limit"] = live_config["memory_limit"]

    return PlatformTelemetry(
        target=target,
        metrics=metrics,
        recent_logs=recent_logs,
        live_config=live_config,
        health_status=health_status,
    )


async def inspect_k8s_live_state(target: TargetResource) -> LiveStateSnapshot:
    """Inspect live state before applying mutations."""
    from nexus.agents import k8s_tools

    try:
        desc = await asyncio.to_thread(
            k8s_tools.describe_resource,
            namespace=target.namespace,
            resource_kind=target.kind,
            resource_name=target.name,
        )
        not_found = "not found" in desc.lower() or "error" in desc.lower()
        is_failing = any(
            m in desc.lower()
            for m in ("crashloopbackoff", "oomkilled", "error", "unhealthy")
        )

        return LiveStateSnapshot(
            target=target,
            exists=not not_found,
            is_failing=is_failing,
            failure_details=desc[:300] if is_failing else None,
            state_attributes={"describe_preview": desc[:500]},
        )
    except Exception as exc:
        return LiveStateSnapshot(
            target=target,
            exists=False,
            is_failing=True,
            failure_details=str(exc),
        )


async def verify_k8s_health(
    target: TargetResource, plan_step: dict[str, Any]
) -> HealthCheckResult:
    """Verify that the target deployment/pod recovered post-remediation."""
    from nexus.agents import k8s_tools

    try:
        desc = await asyncio.to_thread(
            k8s_tools.describe_resource,
            namespace=target.namespace,
            resource_kind=target.kind,
            resource_name=target.name,
        )
        lower_desc = desc.lower()
        is_unhealthy = any(
            bad in lower_desc
            for bad in ("crashloopbackoff", "oomkilled", "error", "evicted", "failed")
        )
        healthy = not is_unhealthy
        return HealthCheckResult(
            healthy=healthy,
            slo_restored=healthy,
            failure_reason=(
                None if healthy else "Unhealthy container status detected in describe"
            ),
            details=desc[:200],
        )
    except Exception as exc:
        return HealthCheckResult(
            healthy=False,
            slo_restored=False,
            failure_reason=str(exc),
        )
