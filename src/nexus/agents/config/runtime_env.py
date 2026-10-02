"""
Runtime Environment Contract Inspection for Pods
"""

from __future__ import annotations

import logging
from typing import Any

from nexus.bus.incident_event import (
    AgentType,
    IncidentEvent,
    Severity,
    SignalType,
)

logger = logging.getLogger(__name__)


def check_runtime_env(
    k8s_core: Any,
    namespaces: list[str],
    required_env_keys: set[str],
) -> list[IncidentEvent]:
    """
    Check that running pods have all required env vars populated.
    """
    events: list[IncidentEvent] = []

    if not required_env_keys or not k8s_core:
        return []

    for ns in namespaces:
        try:
            pods = k8s_core.list_namespaced_pod(
                ns, field_selector="status.phase=Running"
            ).items
        except Exception as exc:
            logger.warning(f"[ConfigAgent] Pod list failed for ns {ns}: {exc}")
            continue

        for pod in pods:
            pod_name = pod.metadata.name
            present_keys: set[str] = set()

            for container in pod.spec.containers or []:
                for env_var in container.env or []:
                    if env_var.name:
                        present_keys.add(env_var.name)

            missing = required_env_keys - present_keys
            for key in missing:
                logger.warning(
                    f"[ConfigAgent] Pod {ns}/{pod_name} missing env key: {key}"
                )
                events.append(
                    IncidentEvent(
                        agent=AgentType.CONFIG,
                        signal_type=SignalType.ENV_KEY_MISSING,
                        severity=Severity.WARNING,
                        namespace=ns,
                        resource_name=pod_name,
                        resource_kind="Pod",
                        context={
                            "pod_name": pod_name,
                            "namespace": ns,
                            "missing_key": key,
                            "note": (
                                "Key may be injected via envFrom (ConfigMap/Secret) — verify manually"
                            ),
                        },
                        suggested_healing_level=0,
                        confidence=0.60,
                    )
                )

    return events
