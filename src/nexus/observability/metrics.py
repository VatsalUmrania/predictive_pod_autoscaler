"""
NEXUS Prometheus Metrics
=========================
Central metric registry for the NEXUS self-healing system.

All NEXUS components call into this module to record measurements.
Metrics are exposed on :9090/metrics in Prometheus text format by the
MetricsServer (status_api.py mounts the same endpoint at :8080/metrics).

Metric naming: nexus_<component>_<measurement>_<unit>

Design:
    • Singleton pattern — `metrics` module-level object, always safe to import
    • No-op stubs when prometheus_client is not installed
    • All update helpers accept Python types (no manual label juggling by callers)

Counters (monotonically increasing):
    nexus_healing_actions_total         per runbook/outcome/level
    nexus_incident_clusters_total       per namespace
    nexus_prescale_decisions_total      per mode
    nexus_rca_requests_total            per source (gemini/rule_based)

Histograms (latency + distribution):
    nexus_confidence_score              at decision time, per failure_class/rca_source
    nexus_rca_duration_seconds          per rca_source
    nexus_feedback_cycle_duration_seconds

Gauges (current snapshot):
    nexus_autonomous_success_rate       30-day rolling
    nexus_false_heal_rate               30-day rolling
    nexus_runbook_success_rate          per runbook_id
    nexus_confidence_adjustment         per runbook_id (KB delta)
    nexus_active_clusters               EventCorrelator queue depth
    nexus_governance_circuit_breaker_open  0=CLOSED, 1=OPEN/tripped
    nexus_prescaler_precision           rolling prediction precision
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# No-op stub (when prometheus_client not installed)
# ──────────────────────────────────────────────────────────────────────────────

class _Noop:
    """Silent no-op replacement for any Prometheus metric."""
    def labels(self, **kwargs) -> "_Noop": return self
    def inc(self, amount: float = 1) -> None: pass
    def observe(self, amount: float) -> None: pass
    def set(self, value: float) -> None: pass
    def time(self):
        from contextlib import contextmanager
        @contextmanager
        def _cm(): yield
        return _cm()


# ──────────────────────────────────────────────────────────────────────────────
# NexusMetrics singleton
# ──────────────────────────────────────────────────────────────────────────────

class NexusMetrics:
    """
    Central Prometheus metric store for NEXUS.

    Usage:
        from nexus.observability.metrics import metrics

        # In RunbookExecutor after outcome:
        metrics.record_healing_action(runbook_id, outcome="success", level=1)

        # In Orchestrator after RCA:
        metrics.record_rca(source="gemini", failure_class="bad_deploy",
                           confidence=0.78, duration_s=1.2)

        # In FeedbackLoop after cycle:
        metrics.update_from_system_kpis(kpis)
    """

    def __init__(self) -> None:
        self._available   = False
        self._registry    = None
        self._initialized = False
        self._lock        = threading.Lock()
        self._init()

    def _init(self) -> None:
        try:
            from prometheus_client import (
                Counter, Gauge, Histogram, CollectorRegistry, CONTENT_TYPE_LATEST
            )
            self._registry = CollectorRegistry()
            self._ct       = CONTENT_TYPE_LATEST
            self._available = True

            # ── Counters ──────────────────────────────────────────────────────
            self.healing_actions_total = Counter(
                "nexus_healing_actions_total",
                "Total healing actions taken",
                ["runbook_id", "outcome", "level"],
                registry=self._registry,
            )
            self.incident_clusters_total = Counter(
                "nexus_incident_clusters_total",
                "Total incident clusters processed",
                ["namespace"],
                registry=self._registry,
            )
            self.prescale_decisions_total = Counter(
                "nexus_prescale_decisions_total",
                "Total pre-scale decisions",
                ["mode"],
                registry=self._registry,
            )
            self.rca_requests_total = Counter(
                "nexus_rca_requests_total",
                "Total RCA analysis requests",
                ["source"],
                registry=self._registry,
            )

            # ── Histograms ────────────────────────────────────────────────────
            self.confidence_score = Histogram(
                "nexus_confidence_score",
                "Distribution of confidence scores at decision time",
                ["failure_class", "rca_source"],
                buckets=[0.30, 0.40, 0.50, 0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.0],
                registry=self._registry,
            )
            self.rca_duration_seconds = Histogram(
                "nexus_rca_duration_seconds",
                "RCA analysis latency in seconds",
                ["rca_source"],
                buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0],
                registry=self._registry,
            )
            self.feedback_cycle_duration_seconds = Histogram(
                "nexus_feedback_cycle_duration_seconds",
                "Duration of one Learning Plane feedback cycle",
                buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
                registry=self._registry,
            )

            # ── Gauges ────────────────────────────────────────────────────────
            self.autonomous_success_rate = Gauge(
                "nexus_autonomous_success_rate",
                "30-day rolling autonomous healing success rate (0.0–1.0)",
                registry=self._registry,
            )
            self.false_heal_rate = Gauge(
                "nexus_false_heal_rate",
                "30-day rolling false-heal rate",
                registry=self._registry,
            )
            self.runbook_success_rate = Gauge(
                "nexus_runbook_success_rate",
                "Per-runbook 30-day success rate from Learning Plane",
                ["runbook_id"],
                registry=self._registry,
            )
            self.confidence_adjustment = Gauge(
                "nexus_confidence_adjustment",
                "Per-runbook confidence delta from KnowledgeBase (−0.10 to +0.05)",
                ["runbook_id"],
                registry=self._registry,
            )
            self.active_clusters = Gauge(
                "nexus_active_clusters",
                "Active incident clusters in EventCorrelator",
                registry=self._registry,
            )
            self.governance_circuit_breaker_open = Gauge(
                "nexus_governance_circuit_breaker_open",
                "Governance circuit breaker: 0=CLOSED (normal), 1=OPEN (tripped)",
                registry=self._registry,
            )
            self.prescaler_precision = Gauge(
                "nexus_prescaler_precision",
                "Rolling precision of Prescaler spike predictions (TP / total)",
                registry=self._registry,
            )

            self._initialized = True
            logger.info("[NexusMetrics] Prometheus metrics initialized")

        except ImportError:
            logger.warning(
                "[NexusMetrics] prometheus_client not installed — metrics disabled. "
                "Install with: pip install prometheus-client"
            )
            self._init_noop()

    def _init_noop(self) -> None:
        noop = _Noop()
        for attr in [
            "healing_actions_total", "incident_clusters_total",
            "prescale_decisions_total", "rca_requests_total",
            "confidence_score", "rca_duration_seconds",
            "feedback_cycle_duration_seconds", "autonomous_success_rate",
            "false_heal_rate", "runbook_success_rate", "confidence_adjustment",
            "active_clusters", "governance_circuit_breaker_open", "prescaler_precision",
        ]:
            setattr(self, attr, noop)

    # ── Prometheus text output ────────────────────────────────────────────────

    def generate_latest(self) -> bytes:
        """Return current metrics in Prometheus text format."""
        if not self._available:
            return b"# prometheus_client not installed\n"
        from prometheus_client import generate_latest as _gen
        return _gen(self._registry)

    @property
    def content_type(self) -> str:
        return getattr(self, "_ct", "text/plain; version=0.0.4; charset=utf-8")

    # ── Convenience update methods (high-level; no label juggling for callers) ──

    def record_healing_action(
        self, runbook_id: str, outcome: str, level: int
    ) -> None:
        """Record one completed healing action (call from RunbookExecutor)."""
        self.healing_actions_total.labels(
            runbook_id=runbook_id, outcome=outcome, level=str(level)
        ).inc()

    def record_rca(
        self,
        source:        str,
        failure_class: str,
        confidence:    float,
        duration_s:    float,
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

    def update_from_runbook_stats(self, all_stats: Dict[str, Any]) -> None:
        """Update per-runbook success rate gauges (call from FeedbackLoop)."""
        for rb_id, stats in all_stats.items():
            try:
                self.runbook_success_rate.labels(runbook_id=rb_id).set(
                    stats.success_rate
                )
            except Exception:
                pass

    def update_from_adjustments(self, adjustments: Dict[str, float]) -> None:
        """Update per-runbook KB adjustment gauges (call from FeedbackLoop)."""
        for rb_id, delta in adjustments.items():
            try:
                self.confidence_adjustment.labels(runbook_id=rb_id).set(delta)
            except Exception:
                pass

    def set_prescaler_precision(self, precision: float) -> None:
        """Update prescaler precision gauge (call from Prescaler / FeedbackLoop)."""
        self.prescaler_precision.set(precision)

    @property
    def is_available(self) -> bool:
        return self._available


# ──────────────────────────────────────────────────────────────────────────────
# Module-level singleton — import this
# ──────────────────────────────────────────────────────────────────────────────

_instance: Optional[NexusMetrics] = None
_singleton_lock = threading.Lock()


def get_metrics() -> NexusMetrics:
    """Return (or create) the global NexusMetrics singleton."""
    global _instance
    if _instance is None:
        with _singleton_lock:
            if _instance is None:
                _instance = NexusMetrics()
    return _instance


# Convenience alias — `from nexus.observability.metrics import metrics`
metrics: NexusMetrics = None  # type: ignore[assignment]


def __getattr__(name: str):   # Module-level __getattr__ (PEP 562)
    global metrics
    if name == "metrics":
        if metrics is None:
            metrics = get_metrics()
        return metrics
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
