"""
NEXUS Triage Node
=================
First node in the zero-hardcoding LangGraph incident pipeline.
Filters internal control-plane noise without fabricating 'default' or
'unknown-service' defaults and without making blocking K8s API calls.
"""

from __future__ import annotations

import logging
from typing import Any

from nexus.graph.state import IncidentState

logger = logging.getLogger(__name__)

_EXCLUDED_NAMESPACES = frozenset(
    {"nexus", "kube-system", "kube-public", "kube-node-lease"}
)
_EXCLUDED_RESOURCE_PREFIXES = ("nexus-api", "nats", "opa", "otel-collector")


def _extract_hint(raw_events: list[dict[str, Any]], *keys: str) -> str | None:
    for ev in raw_events:
        if not isinstance(ev, dict):
            continue
        for k in keys:
            val = ev.get(k)
            if val:
                return str(val)
        ctx = ev.get("context")
        if isinstance(ctx, dict):
            for k in keys:
                val = ctx.get(k)
                if val:
                    return str(val)
        raw = ev.get("raw")
        if isinstance(raw, dict):
            for k in keys:
                val = raw.get(k)
                if val:
                    return str(val)
    return None


async def triage_node(state: IncidentState) -> dict[str, Any]:
    """Triage incoming signals without fabricating missing topology identity."""
    incident_id = state.get("incident_id", "unknown")
    raw_events = list(state.get("raw_events") or [])

    namespace: str | None = state.get("namespace") or _extract_hint(
        raw_events, "namespace", "region_or_namespace"
    )
    service_name: str | None = state.get("service_name") or _extract_hint(
        raw_events, "service_name"
    )
    involved_name: str | None = _extract_hint(
        raw_events, "involved_object_name", "resource_name"
    )
    platform: str | None = state.get("platform") or _extract_hint(
        raw_events, "platform", "provider"
    )
    if platform and platform.lower() in ("kubernetes", "k8s"):
        platform = "k8s"

    # Drop internal NEXUS/Kubernetes control-plane self-events to prevent loops
    ns_lower = (namespace or "").lower()
    res_lower = (service_name or involved_name or "").lower()
    if ns_lower in _EXCLUDED_NAMESPACES or any(
        res_lower.startswith(p) for p in _EXCLUDED_RESOURCE_PREFIXES
    ):
        logger.info(
            "Incident %s ignored during triage (control-plane resource ns=%s res=%s).",
            incident_id,
            namespace,
            res_lower,
        )
        return {
            "triage_status": "IGNORED",
            "status": "IGNORED",
        }

    # Evaluate highest severity across raw_events
    severity = str(state.get("severity") or "warning").lower()
    for ev in raw_events:
        if not isinstance(ev, dict):
            continue
        s = str(ev.get("severity", "")).lower()
        if s in ("critical", "emergency"):
            severity = "critical"
            break
        if s in ("error", "high") and severity not in ("critical",):
            severity = "high"

    logger.info(
        "Incident %s triaged: platform=%s namespace=%s service=%s severity=%s",
        incident_id,
        platform,
        namespace,
        service_name,
        severity,
    )

    return {
        "platform": platform,
        "namespace": namespace,
        "service_name": service_name,
        "raw_events": raw_events,
        "severity": severity,
        "triage_status": "TRIAGED",
        "status": "TRIAGED",
    }
