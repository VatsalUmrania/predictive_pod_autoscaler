"""
Event ingestion, NATS subscription, and cluster flushing helpers for IncidentWorkflow.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from nexus.engine.workflow.state.workflow_state import build_initial_state

logger = logging.getLogger(__name__)

EXCLUDED_NAMESPACES = {"nexus", "kube-system", "kube-public", "kube-node-lease"}
EXCLUDED_RESOURCE_PREFIXES = ("nexus-api", "nats", "opa", "grafana-agent")

__all__ = [
    "build_cluster_event_dicts",
    "handle_incoming_event",
    "run_flush_loop",
    "build_initial_state",
    "setup_event_subscriptions",
]


def build_cluster_event_dicts(cluster: Any) -> list[dict[str, Any]]:
    """Convert events within an IncidentCluster to standardized dictionaries."""
    events_dicts = []
    for e in getattr(cluster, "events", []):
        if hasattr(e, "model_dump"):
            events_dicts.append(e.model_dump())
        elif hasattr(e, "to_dict"):
            events_dicts.append(e.to_dict())
        elif isinstance(e, dict):
            events_dicts.append(e)
        else:
            events_dicts.append(
                {
                    "agent": getattr(getattr(e, "agent", None), "value", "k8s"),
                    "signal_type": getattr(
                        getattr(e, "signal_type", None), "value", "threshold_breach"
                    ),
                    "severity": getattr(
                        getattr(e, "severity", None), "value", "warning"
                    ),
                    "namespace": getattr(e, "namespace", None),
                    "resource_name": getattr(e, "resource_name", None),
                    "resource_kind": getattr(e, "resource_kind", None),
                    "context": getattr(e, "context", {}),
                }
            )
    return events_dicts


def is_control_plane_resource(event: Any) -> bool:
    """Check if event targets NEXUS internal control plane or K8s system components."""
    ns = str(getattr(event, "namespace", "") or "").lower()
    r_name = str(getattr(event, "resource_name", "") or "").lower()
    if ns in EXCLUDED_NAMESPACES:
        return True
    return any(p in r_name for p in EXCLUDED_RESOURCE_PREFIXES)


async def handle_incoming_event(workflow: Any, event: Any) -> None:
    """Handler for single incoming IncidentEvent, dropping stale and control plane events."""
    from nexus.core.bus.events import IncidentEvent

    if isinstance(event, IncidentEvent) and event.timestamp:
        age = datetime.now(timezone.utc) - event.timestamp
        if age > timedelta(seconds=120):
            return

    agent_str = str(getattr(event, "agent", "")).lower()
    if agent_str in ("orchestrator", "nexus"):
        return

    if is_control_plane_resource(event):
        logger.debug(
            "[IncidentWorkflow] Dropping internal control plane event: ns=%s, resource=%s",
            getattr(event, "namespace", ""),
            getattr(event, "resource_name", ""),
        )
        return

    logger.info(
        "[IncidentWorkflow] Ingested event %s: agent=%s, signal=%s, resource=%s",
        getattr(event, "event_id", "unknown"),
        getattr(event, "agent", "unknown"),
        getattr(event, "signal_type", "unknown"),
        getattr(event, "resource_name", "unknown"),
    )

    if workflow.correlator and hasattr(workflow.correlator, "ingest"):
        cluster = workflow.correlator.ingest(event)
        if cluster:
            workflow._incidents_processed += 1
            task = asyncio.create_task(
                workflow._process_cluster(cluster),
                name=f"process-{getattr(cluster, 'cluster_id', 'cluster')}",
            )
            workflow._sub_tasks.append(task)
    else:
        workflow._incidents_processed += 1
        inc_id = getattr(event, "event_id", str(uuid.uuid4()))
        e_dict = (
            event.model_dump()
            if hasattr(event, "model_dump")
            else (
                event.to_dict()
                if hasattr(event, "to_dict")
                else event if isinstance(event, dict) else {}
            )
        )
        inc_task = asyncio.create_task(
            workflow.run_incident(
                {"incident_id": inc_id, "events": [e_dict]}, thread_id=inc_id
            ),
            name=f"process-{inc_id}",
        )
        workflow._sub_tasks.append(inc_task)


async def run_flush_loop(workflow: Any) -> None:
    """Periodically flush stale clusters from correlator."""
    while workflow._running:
        try:
            await asyncio.sleep(workflow._flush_interval)
            if workflow.correlator and hasattr(workflow.correlator, "flush_stale"):
                for cluster in workflow.correlator.flush_stale():
                    workflow._incidents_processed += 1
                    t = asyncio.create_task(
                        workflow._process_cluster(cluster),
                        name=f"flush-{getattr(cluster, 'cluster_id', 'cluster')}",
                    )
                    workflow._sub_tasks.append(t)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("[IncidentWorkflow] Flush loop error: %s", exc)


async def setup_event_subscriptions(workflow: Any) -> None:
    """Setup NATS subscriptions for workflow."""
    if not workflow.nats:
        return
    if hasattr(workflow.nats, "subscribe"):
        try:
            await workflow.nats.subscribe(handler=workflow._on_event, agent_filter=">")
            logger.info("[IncidentWorkflow] Subscribed to NATS nexus.incidents.>")
        except Exception as nats_sub_exc:
            logger.warning("[IncidentWorkflow] NATS subscribe failed: %s", nats_sub_exc)

    if hasattr(workflow.nats, "subscribe_raw"):

        async def _raw_handler(data: dict[str, Any], subject: str = "") -> None:
            inc_id = (
                data.get("incident_id")
                or data.get("event_id")
                or data.get("signal_id")
                or str(uuid.uuid4())
            )
            workflow._incidents_processed += 1
            payload = (
                {"incident_id": inc_id, "anomaly_signal": data, "events": []}
                if subject.startswith("nexus.anomaly.") or "symptom_summary" in data
                else data
            )
            t = asyncio.create_task(workflow.run_incident(payload, thread_id=inc_id))
            workflow._sub_tasks.append(t)

        try:
            await workflow.nats.subscribe_raw("ppa.incidents.>", handler=_raw_handler)
            await workflow.nats.subscribe_raw("nexus.anomaly.>", handler=_raw_handler)
        except Exception as raw_exc:
            logger.debug("[IncidentWorkflow] NATS raw subscription failed: %s", raw_exc)
