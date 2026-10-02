"""
NEXUS Metrics Agent Facade
==========================
Backward-compatibility facade for MetricsAgent.
"""

from nexus.agents.metrics import (
    DEFAULT_QUERIES,
    MetricsAgent,
    PrometheusCircuitBreaker,
)

__all__ = ["MetricsAgent", "PrometheusCircuitBreaker", "DEFAULT_QUERIES"]
