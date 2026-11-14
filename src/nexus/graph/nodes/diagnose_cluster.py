"""
Cluster synthesis helper for diagnose_node.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from nexus.bus.incident_event import AgentType, IncidentEvent, Severity, SignalType
from nexus.reasoning.incident_cluster import IncidentCluster

logger = logging.getLogger(__name__)


def build_diagnose_cluster(
    incident_id: str,
    events: list[Any],
    telemetry: dict[str, Any],
    target: dict[str, Any],
) -> IncidentCluster:
    """Synthesize IncidentCluster from raw events and live telemetry."""
    now = datetime.now(timezone.utc)
    target_name = target.get("name", "unknown")
    cluster = IncidentCluster(
        cluster_id=f"cluster-{incident_id[:8]}",
        created_at=now,
        last_event_at=now,
        events=[],
    )

    for raw_evt in events:
        try:
            if isinstance(raw_evt, IncidentEvent):
                cluster.events.append(raw_evt)
            elif isinstance(raw_evt, dict):
                agent_val = str(raw_evt.get("agent", "k8s")).lower()
                agent_enum = AgentType.K8S
                try:
                    agent_enum = AgentType(agent_val)
                except Exception:
                    agent_enum = AgentType.ORCHESTRATOR

                sev_val = str(raw_evt.get("severity", "warning")).lower()
                sev_enum = (
                    Severity.CRITICAL if sev_val == "critical" else Severity.WARNING
                )

                sig_raw = (
                    str(raw_evt.get("signal_type", "threshold_breach"))
                    .lower()
                    .replace("-", "_")
                )
                sig_enum: SignalType | str
                if sig_raw in (
                    "crashloopbackoff",
                    "crashloop",
                    "crash_loop",
                    "pod_crash_loop",
                ):
                    sig_enum = SignalType.POD_CRASHLOOP
                elif sig_raw in ("oomkilled", "oom_killed", "pod_oom"):
                    sig_enum = SignalType.POD_OOMKILLED
                else:
                    try:
                        sig_enum = SignalType(sig_raw)
                    except ValueError:
                        sig_enum = sig_raw

                evt = IncidentEvent(
                    agent=(
                        agent_enum.value
                        if hasattr(agent_enum, "value")
                        else str(agent_enum)
                    ),
                    signal_type=(
                        sig_enum.value if hasattr(sig_enum, "value") else str(sig_enum)
                    ),
                    severity=(
                        sev_enum.value if hasattr(sev_enum, "value") else str(sev_enum)
                    ),
                    namespace=str(
                        raw_evt.get("namespace") or target.get("namespace", "default")
                    ),
                    resource_name=str(raw_evt.get("resource_name") or target_name),
                )
                cluster.events.append(evt)
        except Exception as evt_exc:
            logger.debug("[Diagnose] Note processing event: %s", evt_exc)

    desc = str(telemetry.get("live_config", {}).get("describe", "")).lower()
    has_deploy = (
        "active rollout=true" in desc
        or "replicasets: 2" in desc
        or "scalingreplicaset" in desc
        or "command:" in desc
        or "command override" in desc
    )
    if has_deploy and "deploy_event" not in cluster.signal_types:
        cluster.events.append(
            IncidentEvent(
                agent=AgentType.K8S,
                signal_type=SignalType.DEPLOY_EVENT,
                severity=Severity.WARNING,
                namespace=str(target.get("namespace", "default")),
                resource_name=str(target_name),
            )
        )

    return cluster
