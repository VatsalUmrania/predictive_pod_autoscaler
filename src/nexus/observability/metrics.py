"""
NEXUS Prometheus Metrics
=========================
Central metric registry for the NEXUS self-healing system.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from nexus.observability.metric_definitions import (
    _Noop,
    create_metrics,
)

logger = logging.getLogger(__name__)

__all__ = ["NexusMetrics", "get_metrics", "metrics", "_Noop"]


class NexusMetrics:
    """Central Prometheus metric store for NEXUS."""

    healing_actions_total: Any
    rca_requests_total: Any
    confidence_score: Any
    rca_duration_seconds: Any
    incident_clusters_total: Any
    prescale_decisions_total: Any
    governance_circuit_breaker_open: Any
    active_clusters: Any
    autonomous_success_rate: Any
    false_heal_rate: Any
    runbook_success_rate: Any
    confidence_adjustment: Any
    prescaler_precision: Any

    def __init__(self) -> None:
        self._available = False
        self._registry: Any = None
        self._ct = "text/plain; version=0.0.4; charset=utf-8"
        self._lock = threading.Lock()
        self._init()

    def _init(self) -> None:
        try:
            from prometheus_client import CollectorRegistry

            self._registry = CollectorRegistry()
        except ImportError:
            self._registry = None

        instruments, self._available, self._ct = create_metrics(self._registry)
        for name, inst in instruments.items():
            setattr(self, name, inst)

    def generate_latest(self) -> bytes:
        """Return current metrics in Prometheus text format."""
        if not self._available or self._registry is None:
            return b"# prometheus_client not installed\n"
        from prometheus_client import generate_latest as _gen

        return _gen(self._registry)

    @property
    def content_type(self) -> str:
        return self._ct

    @property
    def is_available(self) -> bool:
        return self._available

    # ── Convenience update methods ─────────────────────────────────────────────

    def record_healing_action(self, runbook_id: str, outcome: str, level: int) -> None:
        """Record one completed healing action (call from RunbookExecutor)."""
        self.healing_actions_total.labels(
            runbook_id=runbook_id, outcome=outcome, level=str(level)
        ).inc()

    def record_rca(
        self,
        source: str,
        failure_class: str,
        confidence: float,
        duration_s: float,
    ) -> None:
        """Record one RCA result (call from NexusOrchestrator)."""
        self.rca_requests_total.labels(source=source).inc()
        self.confidence_score.labels(
            failure_class=failure_class, rca_source=source
        ).observe(confidence)
        self.rca_duration_seconds.labels(rca_source=source).observe(duration_s)

    def record_cluster(self, namespace: str) -> None:
        """Record that a cluster was processed (call from Orchestrator)."""
        self.incident_clusters_total.labels(namespace=namespace or "unknown").inc()

    def record_prescale(self, mode: str) -> None:
        """Record a prescale decision (call from Prescaler)."""
        self.prescale_decisions_total.labels(mode=mode).inc()

    def set_circuit_breaker(self, is_open: bool) -> None:
        """Update circuit breaker state gauge (call from ActionLadder/GovernanceCB)."""
        self.governance_circuit_breaker_open.set(1.0 if is_open else 0.0)

    def set_active_clusters(self, count: int) -> None:
        """Update active cluster gauge (call from EventCorrelator or Orchestrator)."""
        self.active_clusters.set(float(count))

    def update_from_system_kpis(self, kpis: Any) -> None:
        """Bulk-update gauges from a SystemKPIs object (call from FeedbackLoop)."""
        try:
            self.autonomous_success_rate.set(kpis.autonomous_success_rate)
            self.false_heal_rate.set(kpis.false_heal_rate)
        except Exception as exc:
            logger.debug(f"[NexusMetrics] KPI update error: {exc}")

    def update_from_runbook_stats(self, all_stats: dict[str, Any]) -> None:
        """Update per-runbook success rate gauges (call from FeedbackLoop)."""
        for rb_id, stats in all_stats.items():
            try:
                self.runbook_success_rate.labels(runbook_id=rb_id).set(
                    stats.success_rate
                )
            except Exception:
                pass

    def update_from_adjustments(self, adjustments: dict[str, float]) -> None:
        """Update per-runbook KB adjustment gauges (call from FeedbackLoop)."""
        for rb_id, delta in adjustments.items():
            try:
                self.confidence_adjustment.labels(runbook_id=rb_id).set(delta)
            except Exception:
                pass

    def set_prescaler_precision(self, precision: float) -> None:
        """Update prescaler precision gauge (call from Prescaler / FeedbackLoop)."""
        self.prescaler_precision.set(precision)


# Module-level singleton
_instance: NexusMetrics | None = None
_singleton_lock = threading.Lock()


def get_metrics() -> NexusMetrics:
    """Return (or create) the global NexusMetrics singleton."""
    global _instance
    if _instance is None:
        with _singleton_lock:
            if _instance is None:
                _instance = NexusMetrics()
    return _instance


metrics: NexusMetrics = None  # type: ignore[assignment]


def __getattr__(name: str):
    global metrics
    if name == "metrics":
        if metrics is None:
            metrics = get_metrics()
        return metrics
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
