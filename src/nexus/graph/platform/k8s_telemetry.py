"""
NEXUS Kubernetes Telemetry Collector
====================================
Collects live configuration, spec, and metrics for Kubernetes workloads.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from nexus.graph.platform.base import PlatformTelemetry, TargetResource

logger = logging.getLogger(__name__)


async def collect_k8s_telemetry(target: TargetResource) -> PlatformTelemetry:
    """Collect live telemetry, specs, and status for a Kubernetes target."""
    raw_desc, logs = "", []
    try:
        from nexus.agents import k8s_tools

        raw_desc = k8s_tools.describe_resource(
            namespace=target.namespace,
            resource_kind=target.kind or "deployment",
            resource_name=target.name,
        )
        pod_log = k8s_tools.get_pod_logs(
            namespace=target.namespace, pod_name=target.name
        )
        if pod_log:
            logs.append(pod_log)
    except Exception as exc:
        logger.warning("[K8s Telemetry] Failed to query live cluster: %s", exc)

    live_config: dict[str, Any] = {"raw_describe": raw_desc, "describe": raw_desc}
    metrics: dict[str, Any] = {}

    # Extract replicas
    rep_match = re.search(r"Replicas:\s*(\d+)", raw_desc)
    if rep_match:
        replicas = int(rep_match.group(1))
        live_config["replicas"] = replicas
        metrics["current_replicas"] = replicas

    # Extract limits
    mem_lim = re.search(r"Limits:.*?\bmemory:\s*([0-9]+[A-Za-z]+)", raw_desc, re.DOTALL)
    if mem_lim:
        live_config["memory_limit"] = mem_lim.group(1)
        metrics["memory_limit"] = mem_lim.group(1)

    cpu_lim = re.search(r"Limits:.*?\bcpu:\s*([0-9]+[A-Za-z]+)", raw_desc, re.DOTALL)
    if cpu_lim:
        live_config["cpu_limit"] = cpu_lim.group(1)
        metrics["cpu_limit"] = cpu_lim.group(1)

    # Extract exit code
    exit_match = re.search(r"Exit Code:\s*(\d+)", raw_desc, re.IGNORECASE)
    if exit_match:
        live_config["exit_code"] = int(exit_match.group(1))

    # Health status & command override
    raw_lower = (raw_desc + " " + " ".join(logs)).lower()
    if (
        "echo crashing" in raw_lower
        or "command" in raw_lower
        or live_config.get("exit_code") in (1, 127)
    ):
        live_config["has_command_override"] = True
    if "oomkilled" in raw_lower:
        status = "oomkilled"
    elif "crashloop" in raw_lower:
        status = "crashloopbackoff"
    elif "error" in raw_lower or "failed" in raw_lower:
        status = "degraded"
    else:
        status = "healthy"

    return PlatformTelemetry(
        target=target,
        health_status=status,
        recent_logs=logs,
        live_config=live_config,
        metrics=metrics,
    )
