# operator/metrics.py — Prometheus metrics registry
"""Centralized Prometheus metrics for the PPA Operator.

This module is the single source of truth for all Prometheus metrics
emitted by the operator. Metrics are defined at module level to avoid
duplicate registration errors.

Usage:
    from ppa.operator.metrics import (
        ppa_predicted_load_rps,
        ppa_scale_events_total,
        PpaOperatorMetrics,
    )

    # Or use the bundled accessor:
    metrics = PpaOperatorMetrics.for_cr("my-app", "default")
    metrics.warmup_progress.set(0.5)
"""

from prometheus_client import Counter, Gauge

# Common label names for all CR-scoped metrics
_CR_LABELS = ["cr_name", "namespace"]


# =============================================================================
# Operator Lifecycle Metrics
# Defined in main.py, re-exported here for centralized access
# =============================================================================

# Prediction and scaling state
ppa_predicted_load_rps = Gauge(
    "ppa_predicted_load_rps",
    "LSTM predicted load (req/s)",
    _CR_LABELS,
)

ppa_desired_replicas = Gauge(
    "ppa_desired_replicas",
    "Target replicas",
    _CR_LABELS,
)

ppa_current_replicas = Gauge(
    "ppa_current_replicas",
    "Observed ready replicas",
    _CR_LABELS,
)

ppa_scale_events_total = Counter(
    "ppa_scale_events_total",
    "Total scaling decisions",
    _CR_LABELS,
)

# Model warmup and health
ppa_warmup_progress = Gauge(
    "ppa_warmup_progress",
    "History window fill ratio (0-1)",
    _CR_LABELS,
)

ppa_model_load_failed = Gauge(
    "ppa_model_load_failed",
    "1 if model failed to load, else 0",
    _CR_LABELS,
)


# =============================================================================
# Convenience Class for CR-Scoped Metrics
# =============================================================================


class PpaOperatorMetrics:
    """Convenience accessor for CR-scoped Prometheus metrics.

    Usage:
        metrics = PpaOperatorMetrics.for_cr("my-app", "default")
        metrics.warmup_progress.set(0.5)
        metrics.scale_events.inc()
    """

    def __init__(self, cr_name: str, namespace: str):
        self._cr_name = cr_name
        self._namespace = namespace

    @classmethod
    def for_cr(cls, cr_name: str, namespace: str) -> "PpaOperatorMetrics":
        """Create metrics accessor for a specific CR."""
        return cls(cr_name, namespace)

    @property
    def warmup_progress(self) -> Gauge:
        """Access warmup progress gauge with CR labels."""
        return ppa_warmup_progress.labels(
            cr_name=self._cr_name, namespace=self._namespace
        )

    @property
    def model_load_failed(self) -> Gauge:
        """Access model load failed gauge with CR labels."""
        return ppa_model_load_failed.labels(
            cr_name=self._cr_name, namespace=self._namespace
        )

    @property
    def scale_events(self) -> Counter:
        """Access scale events counter with CR labels."""
        return ppa_scale_events_total.labels(
            cr_name=self._cr_name, namespace=self._namespace
        )

    @property
    def predicted_load(self) -> Gauge:
        """Access predicted load gauge with CR labels."""
        return ppa_predicted_load_rps.labels(
            cr_name=self._cr_name, namespace=self._namespace
        )

    @property
    def desired_replicas(self) -> Gauge:
        """Access desired replicas gauge with CR labels."""
        return ppa_desired_replicas.labels(
            cr_name=self._cr_name, namespace=self._namespace
        )

    @property
    def current_replicas_metric(self) -> Gauge:
        """Access current replicas gauge with CR labels."""
        return ppa_current_replicas.labels(
            cr_name=self._cr_name, namespace=self._namespace
        )


__all__ = [
    # Metrics (raw)
    "ppa_predicted_load_rps",
    "ppa_desired_replicas",
    "ppa_current_replicas",
    "ppa_scale_events_total",
    "ppa_warmup_progress",
    "ppa_model_load_failed",
    # Convenience class
    "PpaOperatorMetrics",
]
