"""
NEXUS Event Correlator
=======================
Groups individual IncidentEvents into IncidentClusters based on
namespace and time-window proximity.

Correlation algorithm (Phase 4 — time-window heuristic):
    • Each namespace has one open cluster at a time
    • A new event extends the open cluster if it arrived within window_s
      of the last event in that cluster
    • If the cluster's window has expired, it is closed and a new one seeded
    • A cluster becomes "ready" when either:
        a) quorum_events events have arrived  →  emitted immediately
        b) flush_timeout_s seconds have elapsed  →  emitted by flush loop

Phase 7 upgrade: Replace time-window heuristic with causal graph analysis
over the telemetry plane (OTel traces provide causal links between events).

Signal types excluded from clustering (purely informational):
    • db_query_spike          — pattern snapshot for DBTrafficCorrelator, not an incident
    • metric_unavailable      — infrastructure health, not a service incident
    • circuit_breaker_tripped — agent health, not a service incident
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from nexus.bus.incident_event import IncidentEvent
from nexus.reasoning.incident_cluster import IncidentCluster

logger = logging.getLogger(__name__)

# Events that carry information but should not trigger RCA on their own
_SKIP_CLUSTERING = frozenset({
    "db_query_spike",
    "metric_unavailable",
    "circuit_breaker_tripped",
    "deploy_event",       # Informational — but will be merged if co-clustered
})

# Single-event signals severe enough to emit a cluster immediately without quorum
_IMMEDIATE_EMIT = frozenset({
    "env_contract_violation",   # Deterministic — always critical, no wait
    "secret_committed",         # Security — always critical, no wait
    "pod_crashloop",            # Clear actionable signal on its own
    "pod_oomkilled",            # Clear actionable signal on its own
    "dns_resolution_failure",   # Dependency failure — time-sensitive
})


class EventCorrelator:
    """
    Clusters temporally related IncidentEvents into IncidentClusters.

    Args:
        correlation_window_s:  Events within this window of the last event are correlated (default 60s).
        quorum_events:         Cluster emits when this many events arrive (default 3).
        flush_timeout_s:       Cluster is force-emitted after this many seconds (default 45s).
    """

    def __init__(
        self,
        correlation_window_s: float = 60.0,
        quorum_events: int = 3,
        flush_timeout_s: float = 45.0,
    ):
        self._window        = correlation_window_s
        self._quorum        = quorum_events
        self._flush_timeout = flush_timeout_s

        # namespace → open IncidentCluster
        self._open: Dict[str, IncidentCluster] = {}

        # De-duplication set (cluster_id → already emitted)
        self._emitted: set = set()

        # Stats
        self._total_ingested  = 0
        self._total_emitted   = 0

    # ── Public API ────────────────────────────────────────────────────────────

    def ingest(self, event: IncidentEvent) -> Optional[IncidentCluster]:
        """
        Ingest a single event synchronously.

        Returns an IncidentCluster when clustering is complete (quorum reached,
        single high-priority signal, or window timeout), otherwise returns None.

        Called from an async NATS handler — must not block.
        """
        signal_type = str(event.signal_type).lower()

        # Skip purely informational events
        if signal_type in _SKIP_CLUSTERING:
            return None

        self._total_ingested += 1
        key = self._cluster_key(event)
        now = datetime.now(timezone.utc)

        # ── Immediate-emit signals ─────────────────────────────────────────────
        # Do NOT cluster these — emit a single-event cluster right away.
        if signal_type in _IMMEDIATE_EMIT:
            cluster = IncidentCluster.new(event)

            # Also absorb any open cluster in the same namespace for context
            open_cluster = self._open.get(key)
            if open_cluster:
                for prior_event in open_cluster.events:
                    cluster.add_event(prior_event)
                del self._open[key]

            return self._emit(cluster)

        # ── Normal correlation ────────────────────────────────────────────────
        cluster = self._open.get(key)

        if cluster is None:
            cluster = IncidentCluster.new(event)
            self._open[key] = cluster
            logger.debug(f"[EventCorrelator] New cluster {cluster.cluster_id} for ns={key}")
            return None

        # Check if the window has expired (time since last event)
        gap_s = (now - cluster.last_event_at).total_seconds()
        if gap_s > self._window:
            # Window expired — close old cluster, start fresh
            old_cluster = self._open.pop(key, None)
            cluster = IncidentCluster.new(event)
            self._open[key] = cluster
            logger.debug(
                f"[EventCorrelator] Window expired ({gap_s:.0f}s) — "
                f"old={old_cluster and old_cluster.cluster_id} new={cluster.cluster_id}"
            )
            # Return the expired cluster if it hasn't been emitted
            if old_cluster and old_cluster.cluster_id not in self._emitted:
                return self._emit(old_cluster)
            return None

        # Extend the existing cluster
        cluster.add_event(event)
        logger.debug(
            f"[EventCorrelator] Added to {cluster.cluster_id} "
            f"({len(cluster.events)} events, ns={key})"
        )

        # Check quorum
        if len(cluster.events) >= self._quorum:
            self._open.pop(key, None)
            if cluster.cluster_id not in self._emitted:
                return self._emit(cluster)

        return None

    def flush_stale(self) -> List[IncidentCluster]:
        """
        Return all clusters that have been open longer than flush_timeout_s.
        Called by the orchestrator's background flush loop every 30s.
        Returns a list of IncidentClusters ready for RCA.
        """
        now    = datetime.now(timezone.utc)
        stale_keys: List[str] = []
        ready:  List[IncidentCluster] = []

        for key, cluster in self._open.items():
            age = (now - cluster.created_at).total_seconds()
            if age >= self._flush_timeout:
                stale_keys.append(key)

        for key in stale_keys:
            cluster = self._open.pop(key, None)
            if cluster and cluster.cluster_id not in self._emitted:
                emitted = self._emit(cluster)
                if emitted:
                    ready.append(emitted)

        if ready:
            logger.info(
                f"[EventCorrelator] Flush: {len(ready)} stale cluster(s) emitted"
            )
        return ready

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _cluster_key(event: IncidentEvent) -> str:
        """Correlation key — events in the same namespace are clustered together."""
        return event.namespace or "global"

    def _emit(self, cluster: IncidentCluster) -> IncidentCluster:
        self._emitted.add(cluster.cluster_id)
        self._total_emitted += 1
        logger.info(
            f"[EventCorrelator] ✦ Cluster ready: {cluster.cluster_id} "
            f"| {len(cluster.events)} events "
            f"| agents=[{', '.join(sorted(cluster.agent_types))}] "
            f"| signals=[{', '.join(sorted(cluster.signal_types))}] "
            f"| severity={cluster.highest_severity}"
        )
        return cluster

    # ── Stats ─────────────────────────────────────────────────────────────────

    @property
    def stats(self) -> dict:
        return {
            "total_ingested":  self._total_ingested,
            "total_emitted":   self._total_emitted,
            "open_clusters":   len(self._open),
            "correlation_window_s": self._window,
            "quorum_events":   self._quorum,
            "flush_timeout_s": self._flush_timeout,
        }
