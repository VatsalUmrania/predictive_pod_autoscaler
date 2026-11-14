"""
Kubernetes Pod Health Inspection Logic
"""

from __future__ import annotations

import time
from typing import Any

from nexus.agents.k8s.constants import (
    get_container_memory_limit,
    get_deployment_name,
)
from nexus.bus.incident_event import (
    AgentType,
    IncidentEvent,
    PodFailureContext,
    Severity,
    SignalType,
)


def check_pod(
    pod: Any,
    pending_since: dict[str, float],
    emitted_this_cycle: set[str],
    pending_threshold_s: float,
    crashloop_threshold: int,
) -> list[IncidentEvent]:
    """Inspect a single pod object for failure conditions."""
    events: list[IncidentEvent] = []
    name = pod.metadata.name
    ns = pod.metadata.namespace
    key = f"{ns}/{name}"

    if key in emitted_this_cycle:
        return []

    phase = (pod.status.phase or "").lower()

    # Pending too long
    if phase == "pending":
        now = time.monotonic()
        if key not in pending_since:
            pending_since[key] = now
        elif (now - pending_since[key]) > pending_threshold_s:
            emitted_this_cycle.add(key)
            events.append(
                IncidentEvent(
                    agent=AgentType.K8S,
                    signal_type=SignalType.POD_PENDING,
                    severity=Severity.WARNING,
                    namespace=ns,
                    resource_name=name,
                    resource_kind="Pod",
                    context={
                        "pod_name": name,
                        "pending_minutes": (now - pending_since[key]) / 60.0,
                    },
                )
            )
    else:
        pending_since.pop(key, None)

    # Container statuses
    for cs in pod.status.container_statuses or []:
        restart_count = cs.restart_count or 0

        # CrashLoopBackOff
        waiting_reason = ""
        if cs.state and cs.state.waiting:
            waiting_reason = cs.state.waiting.reason or ""

        if (
            waiting_reason == "CrashLoopBackOff"
            and restart_count >= crashloop_threshold
        ):
            emitted_this_cycle.add(key)
            deployment_name = get_deployment_name(pod) or name
            events.append(
                IncidentEvent(
                    agent=AgentType.K8S,
                    signal_type=SignalType.POD_CRASHLOOP,
                    severity=(
                        Severity.CRITICAL if restart_count >= 5 else Severity.WARNING
                    ),
                    namespace=ns,
                    resource_name=deployment_name,
                    resource_kind="Deployment",
                    context=PodFailureContext(
                        pod_name=name,
                        deployment_name=deployment_name,
                        restart_count=restart_count,
                        reason="CrashLoopBackOff",
                    ).model_dump(),
                    suggested_healing_level=1,
                    confidence=0.95,
                )
            )

        # OOMKilled
        elif (
            cs.last_state
            and cs.last_state.terminated
            and cs.last_state.terminated.reason == "OOMKilled"
        ):
            emitted_this_cycle.add(key)
            deployment_name = get_deployment_name(pod) or name
            mem_limit = get_container_memory_limit(pod, cs.name)

            events.append(
                IncidentEvent(
                    agent=AgentType.K8S,
                    signal_type=SignalType.POD_OOMKILLED,
                    severity=Severity.WARNING,
                    namespace=ns,
                    resource_name=deployment_name,
                    resource_kind="Deployment",
                    context=PodFailureContext(
                        pod_name=name,
                        deployment_name=deployment_name,
                        restart_count=restart_count,
                        exit_code=137,
                        reason="OOMKilled",
                        memory_limit_mi=mem_limit,
                    ).model_dump(),
                    suggested_healing_level=1,
                    confidence=0.95,
                )
            )

    return events
