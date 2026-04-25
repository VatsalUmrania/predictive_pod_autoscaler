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
from typing import Any, Dict, List, Optional, Set

from nexus.bus.incident_event import IncidentEvent

# Severity ordering map
_SEV_ORDER: Dict[str, int] = {"info": 0, "warning": 1, "critical": 2, "emergency": 3}


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
    events: List[IncidentEvent] = field(default_factory=list)

    # ── Computed properties ───────────────────────────────────────────────────

    @property
    def namespace(self) -> Optional[str]:
        """Most frequently referenced namespace across all events."""
        counts: Dict[str, int] = {}
        for e in self.events:
            if e.namespace:
                counts[e.namespace] = counts.get(e.namespace, 0) + 1
        return max(counts, key=counts.get) if counts else None

    @property
    def primary_resource(self) -> Optional[str]:
        """Most frequently referenced resource name."""
        counts: Dict[str, int] = {}
        for e in self.events:
            if e.resource_name:
                counts[e.resource_name] = counts.get(e.resource_name, 0) + 1
        return max(counts, key=counts.get) if counts else None

    @property
    def signal_types(self) -> Set[str]:
        return {str(e.signal_type) for e in self.events}

    @property
    def agent_types(self) -> Set[str]:
        return {str(e.agent) for e in self.events}

    @property
    def age_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.created_at).total_seconds()

    @property
    def has_deploy_event(self) -> bool:
        return any("deploy" in st.lower() for st in self.signal_types)

    @property
    def has_env_violation(self) -> bool:
        return any("env" in st.lower() and "violation" in st.lower() for st in self.signal_types)

    @property
    def highest_severity(self) -> str:
        best = "info"
        best_level = 0
        for e in self.events:
            sev   = str(e.severity).lower()
            level = _SEV_ORDER.get(sev, 0)
            if level > best_level:
                best_level = level
                best = sev
        return best

    @property
    def most_critical_event(self) -> Optional[IncidentEvent]:
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
        """
        Measures how strongly independent signals agree on a root cause (0.0–1.0).

        Factors:
          - Agent diversity:  events from different agents = stronger evidence
          - Event count:      more events = higher confidence
          - Average anomaly score from event contexts (when available)
        """
        if not self.events:
            return 0.0
        if len(self.events) == 1:
            return 0.35   # Single signal — conservative

        agent_count = len(self.agent_types)
        event_count = len(self.events)

        # Agent diversity: 5+ independent agents = full score
        diversity = min(agent_count / 5.0, 1.0)
        # Event volume: 5+ events = full score
        volume = min(event_count / 5.0, 1.0)

        # Pull anomaly_score from event context if set
        anomaly_scores = [
            float(e.context.get("anomaly_score", 0.5))
            for e in self.events
            if isinstance(e.context, dict) and "anomaly_score" in e.context
        ]
        avg_anomaly = (sum(anomaly_scores) / len(anomaly_scores)) if anomaly_scores else 0.5

        return diversity * 0.40 + volume * 0.30 + avg_anomaly * 0.30

    # ── Context rendering for LLM ─────────────────────────────────────────────

    def to_llm_context(self) -> str:
        """
        Render a compact incident report suitable for inclusion in an LLM prompt.
        Keeps token usage tight — no redundant fields.
        """
        lines = [
            f"INCIDENT CLUSTER: {self.cluster_id}",
            f"Detected: {self.created_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
            f"Open for: {self.age_seconds:.0f}s",
            f"Namespace: {self.namespace or 'unknown'}",
            f"Primary resource: {self.primary_resource or 'unknown'}",
            f"Highest severity: {self.highest_severity.upper()}",
            f"Total signals: {len(self.events)} from {len(self.agent_types)} agent(s)",
            "",
            "SIGNALS:",
        ]

        for i, event in enumerate(self.events, 1):
            ctx_kv = ""
            if isinstance(event.context, dict):
                _KEY_ORDER = [
                    "error_rate", "current_value", "threshold", "anomaly_score",
                    "latency_p95_ms", "restart_count", "missing_keys",
                    "sha", "author", "reason", "utilization_pct", "active_connections",
                ]
                relevant = {
                    k: event.context[k]
                    for k in _KEY_ORDER
                    if k in event.context
                }[:3]
                if relevant:
                    parts = []
                    for k, v in relevant.items():
                        if isinstance(v, float):
                            parts.append(f"{k}={v:.3f}")
                        elif isinstance(v, list):
                            parts.append(f"{k}=[{','.join(str(x) for x in v[:3])}]")
                        else:
                            parts.append(f"{k}={v}")
                    ctx_kv = "  |  " + "  ".join(parts)

            lines.append(
                f"  {i:2d}. [{str(event.agent).upper():<14s}] "
                f"{str(event.signal_type).upper():<30s} "
                f"sev={str(event.severity).lower()}"
                f"{ctx_kv}"
            )

        # Deploy event detail block
        if self.has_deploy_event:
            for e in self.events:
                if "deploy" in str(e.signal_type).lower() and isinstance(e.context, dict):
                    sha    = e.context.get("sha", "unknown")
                    author = e.context.get("author", "unknown")
                    lines.append(f"\nRECENT DEPLOY: sha={str(sha)[:12]}  by={author}")
                    break

        return "\n".join(lines)

    def to_summary(self) -> Dict[str, Any]:
        return {
            "cluster_id":        self.cluster_id,
            "created_at":        self.created_at.isoformat(),
            "age_seconds":       round(self.age_seconds, 1),
            "event_count":       len(self.events),
            "agent_types":       sorted(self.agent_types),
            "signal_types":      sorted(self.signal_types),
            "namespace":         self.namespace,
            "primary_resource":  self.primary_resource,
            "highest_severity":  self.highest_severity,
            "has_deploy_event":  self.has_deploy_event,
            "signal_agreement":  round(self.signal_agreement_score(), 3),
        }

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def new(cls, first_event: IncidentEvent) -> "IncidentCluster":
        """Create a new cluster seeded with one event."""
        ts  = datetime.now(timezone.utc)
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
