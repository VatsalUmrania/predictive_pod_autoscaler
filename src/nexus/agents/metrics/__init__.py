"""
NEXUS Metrics Agent Package
"""

from nexus.agents.metrics.agent import MetricsAgent
from nexus.agents.metrics.circuit_breaker import PrometheusCircuitBreaker
from nexus.agents.metrics.queries import DEFAULT_QUERIES

__all__ = ["MetricsAgent", "PrometheusCircuitBreaker", "DEFAULT_QUERIES"]
