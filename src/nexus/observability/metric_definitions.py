"""Prometheus Metric Definitions and No-op Stubs for NEXUS."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)


class _Noop:
    """Silent no-op replacement for any Prometheus metric."""

    def labels(self, **kwargs) -> _Noop:
        return self

    def inc(self, amount: float = 1) -> None:
        pass

    def observe(self, amount: float) -> None:
        pass

    def set(self, value: float) -> None:
        pass

    def time(self):
        @contextmanager
        def _cm():
            yield

        return _cm()


METRIC_NAMES = [
    "healing_actions_total",
    "incident_clusters_total",
    "prescale_decisions_total",
    "rca_requests_total",
    "confidence_score",
    "rca_duration_seconds",
    "feedback_cycle_duration_seconds",
    "autonomous_success_rate",
    "false_heal_rate",
    "runbook_success_rate",
    "confidence_adjustment",
    "active_clusters",
    "governance_circuit_breaker_open",
    "prescaler_precision",
]


def create_metrics(registry: Any) -> tuple[dict[str, Any], bool, str]:
    """Create all Prometheus metric instruments, falling back to no-ops if unavailable."""
    try:
        from prometheus_client import (
            CONTENT_TYPE_LATEST,
            Counter,
            Gauge,
            Histogram,
        )

        d: dict[str, Any] = {
            "healing_actions_total": Counter(
                "nexus_healing_actions_total",
                "Total healing actions taken",
                ["runbook_id", "outcome", "level"],
                registry=registry,
            ),
            "incident_clusters_total": Counter(
                "nexus_incident_clusters_total",
                "Total incident clusters processed",
                ["namespace"],
                registry=registry,
            ),
            "prescale_decisions_total": Counter(
                "nexus_prescale_decisions_total",
                "Total pre-scale decisions",
                ["mode"],
                registry=registry,
            ),
            "rca_requests_total": Counter(
                "nexus_rca_requests_total",
                "Total RCA analysis requests",
                ["source"],
                registry=registry,
            ),
            "confidence_score": Histogram(
                "nexus_confidence_score",
                "Distribution of confidence scores at decision time",
                ["failure_class", "rca_source"],
                buckets=[0.30, 0.40, 0.50, 0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.0],
                registry=registry,
            ),
            "rca_duration_seconds": Histogram(
                "nexus_rca_duration_seconds",
                "RCA analysis latency in seconds",
                ["rca_source"],
                buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0],
                registry=registry,
            ),
            "feedback_cycle_duration_seconds": Histogram(
                "nexus_feedback_cycle_duration_seconds",
                "Duration of one Learning Plane feedback cycle",
                buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
                registry=registry,
            ),
            "autonomous_success_rate": Gauge(
                "nexus_autonomous_success_rate",
                "30-day rolling autonomous healing success rate (0.0–1.0)",
                registry=registry,
            ),
            "false_heal_rate": Gauge(
                "nexus_false_heal_rate",
                "30-day rolling false-heal rate",
                registry=registry,
            ),
            "runbook_success_rate": Gauge(
                "nexus_runbook_success_rate",
                "Per-runbook 30-day success rate from Learning Plane",
                ["runbook_id"],
                registry=registry,
            ),
            "confidence_adjustment": Gauge(
                "nexus_confidence_adjustment",
                "Per-runbook confidence delta from KnowledgeBase (−0.10 to +0.05)",
                ["runbook_id"],
                registry=registry,
            ),
            "active_clusters": Gauge(
                "nexus_active_clusters",
                "Active incident clusters in EventCorrelator",
                registry=registry,
            ),
            "governance_circuit_breaker_open": Gauge(
                "nexus_governance_circuit_breaker_open",
                "Governance circuit breaker: 0=CLOSED (normal), 1=OPEN (tripped)",
                registry=registry,
            ),
            "prescaler_precision": Gauge(
                "nexus_prescaler_precision",
                "Rolling precision of Prescaler spike predictions (TP / total)",
                registry=registry,
            ),
        }
        logger.info("[NexusMetrics] Prometheus metrics initialized")
        return d, True, CONTENT_TYPE_LATEST
    except ImportError:
        logger.warning(
            "[NexusMetrics] prometheus_client not installed — metrics disabled."
        )
        noop = _Noop()
        return {name: noop for name in METRIC_NAMES}, False, "text/plain; version=0.0.4; charset=utf-8"
