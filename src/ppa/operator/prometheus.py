"""Prometheus metric queries — Re-export from infrastructure (backward compatibility).

This module re-exports Prometheus client from infrastructure for backward compatibility.
All implementations now live in ppa.infrastructure.prometheus.

New code should import directly from infrastructure:
    from ppa.infrastructure import prom_query, prom_query_parallel
    from ppa.infrastructure.prometheus import PrometheusCircuitBreakerError

Existing code can continue using this module:
    from ppa.operator.prometheus import prom_query  # Still works!
"""

# Re-export all Prometheus client functions and exceptions for backward compatibility
from ppa.infrastructure.prometheus import (
    PrometheusCircuitBreakerError,
    PrometheusCircuitBreakerTripped,
    get_current_prometheus_url,
    prom_query,
    prom_query_parallel,
    set_prometheus_url,
)

__all__ = [
    "PrometheusCircuitBreakerError",
    "PrometheusCircuitBreakerTripped",
    "prom_query",
    "prom_query_parallel",
    "set_prometheus_url",
    "get_current_prometheus_url",
]
