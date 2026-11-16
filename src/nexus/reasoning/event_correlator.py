"""
NEXUS Event Correlator
=======================
Groups individual IncidentEvents into IncidentClusters based on
namespace and time-window proximity.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone

from nexus.bus.incident_event import IncidentEvent, SignalType
from nexus.reasoning.incident_cluster import IncidentCluster

logger = logging.getLogger(__name__)

_SKIP_CLUSTERING = frozenset(
    {"db_query_spike", "metric_unavailable", "circuit_breaker_tripped", "deploy_event"}
)
_IMMEDIATE_EMIT = frozenset(
    {
        "env_contract_violation",
        "secret_committed",
        "pod_crashloop",
        "pod_oomkilled",
        "dns_resolution_failure",
    }
)


class EventCorrelator:
    """Clusters temporally related IncidentEvents into IncidentClusters."""

    def __init__(
        self,
        correlation_window_s: float = 60.0,
        quorum_events: int = 3,
        flush_timeout_s: float = 45.0,
    ):
        self._window = correlation_window_s
        self._quorum = quorum_events
        self._flush_timeout = flush_timeout_s

        # namespace → open IncidentCluster
        self._open: dict[str, IncidentCluster] = {}

        # De-duplication set (cluster_id → already emitted)
        self._emitted: set[str] = set()
        self._emitted_order: deque[str] = deque()
        self._max_emitted = 500

        # Stats
        self._total_ingested = 0
        self._total_emitted = 0

    # Public API
    def ingest(self, event: IncidentEvent) -> IncidentCluster | None:
        """Ingest an event synchronously. Returns IncidentCluster if ready."""

        signal_type = str(event.signal_type).lower()

        # Skip purely informational events
        if signal_type in _SKIP_CLUSTERING:
            return None

        self._total_ingested += 1
        key = self._cluster_key(event)
        now = datetime.now(timezone.utc)

        # Immediate-emit signals
        # Do NOT cluster these — emit a single-event cluster right away.
        if signal_type in _IMMEDIATE_EMIT:
            imm_cluster = IncidentCluster.new(event)

            # Also absorb any open cluster in the same namespace for context
            open_cluster = self._open.get(key)
            if open_cluster:
                for prior_event in open_cluster.events:
                    imm_cluster.add_event(prior_event)
                del self._open[key]

            return self._emit(imm_cluster)

        # Normal correlation
        cluster = self._open.get(key)

        if cluster is None:
            cluster = IncidentCluster.new(event)
            self._open[key] = cluster
            logger.debug(
                f"[EventCorrelator] New cluster {cluster.cluster_id} for ns={key}"
            )
            self._propagate_ppa(cluster, event)
            return None

        # Check if the window has expired (time since last event)
        gap_s = (now - cluster.last_event_at).total_seconds()
        if gap_s > self._window:
            # Window expired — close old cluster, start fresh
            old_cluster = self._open.pop(key, None)
            cluster = IncidentCluster.new(event)
            self._open[key] = cluster
            self._propagate_ppa(cluster, event)
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

    def flush_stale(self) -> list[IncidentCluster]:
        """
        Return all clusters that have been open longer than flush_timeout_s.
        Called by the orchestrator's background flush loop every 30s.
        Returns a list of IncidentClusters ready for RCA.
        """
        now = datetime.now(timezone.utc)
        stale_keys: list[str] = []
        ready: list[IncidentCluster] = []

        for key, cluster in self._open.items():
            age = (now - cluster.created_at).total_seconds()
            if age >= self._flush_timeout:
                stale_keys.append(key)

        for key in stale_keys:
            c = self._open.pop(key, None)
            if c and c.cluster_id not in self._emitted:
                emitted = self._emit(c)
                if emitted:
                    ready.append(emitted)

        if ready:
            logger.info(
                f"[EventCorrelator] Flush: {len(ready)} stale cluster(s) emitted"
            )
        return ready

    @staticmethod
    def _propagate_ppa(cluster: IncidentCluster, event: IncidentEvent) -> None:
        if (
            isinstance(event.context, dict)
            and "raw_features" in event.context
            and event.signal_type == SignalType.TRAFFIC_SPIKE_PREDICTED
        ):
            cluster.ppa_raw_features = event.context["raw_features"]

    @staticmethod
    def _cluster_key(event: IncidentEvent) -> str:
        """Correlation key — events in the same namespace are clustered together."""
        return event.namespace if event.namespace else f"global:{event.agent}"

    def _emit(self, cluster: IncidentCluster) -> IncidentCluster:
        self._emitted.add(cluster.cluster_id)
        self._emitted_order.append(cluster.cluster_id)
        if len(self._emitted_order) > self._max_emitted:
            oldest = self._emitted_order.popleft()
            self._emitted.discard(oldest)
        self._total_emitted += 1
        agents = ", ".join(sorted(cluster.agent_types))
        logger.info(
            f"[EventCorrelator] Cluster ready: {cluster.cluster_id} | {len(cluster.events)} events | agents=[{agents}] | severity={cluster.highest_severity}"
        )
        return cluster

    # Stats
    @property
    def stats(self) -> dict:
        return {
            "total_ingested": self._total_ingested,
            "total_emitted": self._total_emitted,
            "open_clusters": len(self._open),
            "correlation_window_s": self._window,
            "quorum_events": self._quorum,
            "flush_timeout_s": self._flush_timeout,
        }
