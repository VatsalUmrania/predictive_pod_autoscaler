"""
NEXUS Incident Cluster
=======================
A temporally correlated group of IncidentEvents that together describe one incident.

The EventCorrelator builds clusters by grouping events from multiple agents
that arrive within a time window and share the same namespace context.

Clusters are the unit of work for the RCA Engine:

    [MetricsAgent: HIGH_ERROR_RATE]   ─┐
    [K8sAgent:     POD_CRASHLOOP     ] ─┤→ IncidentCluster → RCAEngine → Orchestrator
    [NginxAgent:   HIGH_ERROR_RATE   ] ─┘

A cluster is more informative than a single event because it captures
signal agreement — multiple independent agents observing the same failure
provides high-confidence evidence for root cause attribution.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from nexus.core.bus.events import IncidentEvent

# Severity ordering map
_SEV_ORDER: dict[str, int] = {
    "info": 0,
    "low": 0,
    "medium": 1,
    "warning": 1,
    "error": 2,
    "high": 2,
    "critical": 3,
    "emergency": 4,
}


def extract_event_identity_key(event: IncidentEvent) -> str | None:
    """
    Extract a stable resource/service identity key for correlation when
    service_name/resource_name is None, falling back to involved_object_name
    from context or raw_payload.
    """
    if event.resource_name:
        return event.resource_name

    ctx = event.context if isinstance(event.context, dict) else {}
    for key in ("service_name", "involved_object_name"):
        val = ctx.get(key)
        if val:
            return str(val)

    raw_candidates = []
    if isinstance(event.raw, dict):
        raw_candidates.append(event.raw)
    if isinstance(ctx.get("raw"), dict):
        raw_candidates.append(ctx["raw"])

    for raw in raw_candidates:
        for key in ("service_name", "involved_object_name"):
            if raw.get(key):
                return str(raw[key])
        raw_attrs = raw.get("raw_attributes")
        if isinstance(raw_attrs, dict):
            for key in (
                "k8s.deployment.name",
                "service.name",
                "k8s.involved.name",
                "k8s.pod.name",
            ):
                if raw_attrs.get(key):
                    return str(raw_attrs[key])
        body = raw.get("raw_body") if "raw_body" in raw else raw.get("body")
        if isinstance(body, dict):
            obj = body.get("object") if isinstance(body.get("object"), dict) else body
            if isinstance(obj, dict):
                involved = obj.get("involvedObject")
                if isinstance(involved, dict) and involved.get("name"):
                    return str(involved["name"])
    return None


@dataclass
class IncidentCluster:
    """
    A temporally correlated group of IncidentEvents.

    Attributes:
        cluster_id:    Unique, deterministic ID (``CL-<8-char-hex>``).
        created_at:    Timestamp of the first event.
        last_event_at: Timestamp of the most recently added event.
        events:        All correlated IncidentEvents, in arrival order.
    """

    cluster_id: str
    created_at: datetime
    last_event_at: datetime
    events: list[IncidentEvent] = field(default_factory=list)
    ppa_raw_features: dict[str, float] | None = None

    # ── Computed properties ───────────────────────────────────────────────────

    @property
    def namespace(self) -> str | None:
        """Most frequently referenced namespace across all events."""
        counts: dict[str, int] = {}
        for e in self.events:
            if e.namespace:
                counts[e.namespace] = counts.get(e.namespace, 0) + 1
        return max(counts, key=counts.__getitem__) if counts else None

    @property
    def primary_resource(self) -> str | None:
        """Most frequently referenced resource name (falling back to involved_object_name)."""
        counts: dict[str, int] = {}
        for e in self.events:
            ident = extract_event_identity_key(e)
            if ident:
                counts[ident] = counts.get(ident, 0) + 1
        return max(counts, key=counts.__getitem__) if counts else None

    @property
    def signal_types(self) -> set[str]:
        res = set()
        for e in self.events:
            st = getattr(e, "signal_type", None)
            if st is not None:
                if hasattr(st, "value"):
                    res.add(str(st.value).lower())
                else:
                    s = str(st).lower()
                    res.add(s.split(".")[-1] if "." in s else s)
        return res

    @property
    def agent_types(self) -> set[str]:
        res = set()
        for e in self.events:
            ag = getattr(e, "agent", None)
            if ag is not None:
                if hasattr(ag, "value"):
                    res.add(str(ag.value).lower())
                else:
                    s = str(ag).lower()
                    res.add(s.split(".")[-1] if "." in s else s)
        return res

    @property
    def age_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.created_at).total_seconds()

    @property
    def fingerprint(self) -> str:
        """Deterministic fingerprint representing the incident cluster target."""
        ns = self.namespace or "unscoped"
        res = self.primary_resource or self.cluster_id
        return f"{ns}:{res}"

    @property
    def has_deploy_event(self) -> bool:
        return any("deploy" in st.lower() for st in self.signal_types)

    @property
    def has_env_violation(self) -> bool:
        return any(
            "env" in st.lower() and "violation" in st.lower()
            for st in self.signal_types
        )

    @property
    def highest_severity(self) -> str:
        best = "info"
        best_level = 0
        for e in self.events:
            sev = str(e.severity).lower()
            level = _SEV_ORDER.get(sev, 0)
            if level > best_level:
                best_level = level
                best = sev
        return best

    @property
    def most_critical_event(self) -> IncidentEvent | None:
        """The event with the highest severity; ties broken by arrival order (last wins)."""
        if not self.events:
            return None
        return max(
            self.events,
            key=lambda e: _SEV_ORDER.get(str(e.severity).lower(), 0),
        )

    # ── Mutation ──────────────────────────────────────────────────────────────

    def add_event(self, event: IncidentEvent) -> None:
        self.events.append(event)
        self.last_event_at = datetime.now(timezone.utc)

    # ── Scoring helpers ───────────────────────────────────────────────────────

    def signal_agreement_score(self) -> float:
        """Measures how strongly independent signals agree on a root cause (0.0–1.0)."""
        from nexus.engine.reasoning.cluster_formatter import compute_signal_agreement

        return compute_signal_agreement(self)

    # ── Context rendering for LLM ─────────────────────────────────────────────

    def to_llm_context(self) -> str:
        """
        Render a compact incident report suitable for inclusion in an LLM prompt.
        Keeps token usage tight — no redundant fields.
        """
        from nexus.engine.reasoning.cluster_formatter import format_llm_context

        return format_llm_context(self)

    def to_summary(self) -> dict[str, Any]:
        from nexus.engine.reasoning.cluster_formatter import format_cluster_summary

        return format_cluster_summary(self)

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def new(cls, first_event: IncidentEvent) -> IncidentCluster:
        """Create a new cluster seeded with one event."""
        ts = datetime.now(timezone.utc)
        key = f"{first_event.namespace or 'global'}:{ts.isoformat()}"
        cid = "CL-" + hashlib.sha256(key.encode()).hexdigest()[:8].upper()

        cluster = cls(cluster_id=cid, created_at=ts, last_event_at=ts)
        cluster.add_event(first_event)
        return cluster

    def __repr__(self) -> str:
        return (
            f"IncidentCluster({self.cluster_id}, ns={self.namespace}, "
            f"events={len(self.events)}, agents={sorted(self.agent_types)})"
        )
