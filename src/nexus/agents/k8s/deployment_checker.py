"""
Kubernetes Deployment & HPA Inspection Logic
"""

from __future__ import annotations

import logging
import time
from typing import Any

from nexus.bus.incident_event import (
    AgentType,
    IncidentEvent,
    Severity,
    SignalType,
)

logger = logging.getLogger(__name__)


def check_deployment(
    dep: Any,
    degraded_since: dict[str, float],
    emitted_this_cycle: set[str],
    degraded_threshold_s: float,
) -> list[IncidentEvent]:
    """
    Inspect a single deployment for failure conditions.
    """
    events: list[IncidentEvent] = []
    name = dep.metadata.name
    ns = dep.metadata.namespace
    key = f"{ns}/{name}"
    spec = dep.spec
    status = dep.status

    desired = spec.replicas if spec.replicas is not None else 1
    available = status.available_replicas or 0
    ready = status.ready_replicas or 0

    if available < desired and desired > 0:
        now = time.monotonic()
        if key not in degraded_since:
            degraded_since[key] = now
            logger.debug(
                f"[K8sAgent] Deployment {key} degraded "
                f"(available={available}/{desired}) — starting grace timer"
            )
        elif (now - degraded_since[key]) > degraded_threshold_s:
            if key not in emitted_this_cycle:
                emitted_this_cycle.add(key)
                degraded_for_s = now - degraded_since[key]
                events.append(
                    IncidentEvent(
                        agent=AgentType.K8S,
                        signal_type=SignalType.DEPLOYMENT_DEGRADED,
                        severity=(
                            Severity.CRITICAL if available == 0 else Severity.WARNING
                        ),
                        namespace=ns,
                        resource_name=name,
                        resource_kind="Deployment",
                        context={
                            "deployment": name,
                            "desired_replicas": desired,
                            "available_replicas": available,
                            "ready_replicas": ready,
                            "degraded_for_seconds": round(degraded_for_s, 1),
                        },
                    )
                )
    else:
        degraded_since.pop(key, None)

    for cond in status.conditions or []:
        if (
            cond.type == "Progressing"
            and cond.status == "False"
            and cond.reason == "ProgressDeadlineExceeded"
            and key not in emitted_this_cycle
        ):
            emitted_this_cycle.add(key)
            events.append(
                IncidentEvent(
                    agent=AgentType.K8S,
                    signal_type=SignalType.ROLLOUT_STUCK,
                    severity=Severity.CRITICAL,
                    namespace=ns,
                    resource_name=name,
                    resource_kind="Deployment",
                    context={
                        "deployment": name,
                        "condition_reason": cond.reason,
                        "condition_message": cond.message,
                    },
                )
            )
    return events


def check_hpa(
    hpa: Any,
    hpa_maxed_since: dict[str, float],
    hpa_maxed_threshold_s: float,
) -> list[IncidentEvent]:
    """Inspect an HPA object for maxed replica condition."""
    events: list[IncidentEvent] = []
    name = hpa.metadata.name
    ns = hpa.metadata.namespace
    key = f"{ns}/{name}"
    status = hpa.status
    spec = hpa.spec

    current = status.current_replicas or 0
    maximum = spec.max_replicas or 0

    if current >= maximum > 0:
        now = time.monotonic()
        if key not in hpa_maxed_since:
            hpa_maxed_since[key] = now
        elif (now - hpa_maxed_since[key]) > hpa_maxed_threshold_s:
            events.append(
                IncidentEvent(
                    agent=AgentType.K8S,
                    signal_type=SignalType.HPA_MAXED,
                    severity=Severity.CRITICAL,
                    namespace=ns,
                    resource_name=name,
                    resource_kind="HorizontalPodAutoscaler",
                    context={
                        "hpa_name": name,
                        "current_replicas": current,
                        "max_replicas": maximum,
                        "maxed_minutes": (now - hpa_maxed_since[key]) / 60.0,
                    },
                )
            )
    else:
        hpa_maxed_since.pop(key, None)
    return events
