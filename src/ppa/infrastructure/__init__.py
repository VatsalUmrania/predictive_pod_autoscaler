"""Infrastructure adapters for external systems (K8s, Prometheus, etc.).

This module provides clean adapters for infrastructure concerns:
- kubernetes: Kubernetes API client and deployment scaling
- prometheus: Prometheus query execution with circuit breaker resilience

These adapters are independent of domain logic and can be tested/mocked in isolation.

Architecture:
- domain/: Pure logic (math, validation, state) — testable without infrastructure
- infrastructure/: External adapters (K8s, Prometheus) — infrastructure integration
- operator/: Orchestration layer using both domain and infrastructure
"""

from ppa.infrastructure.kubernetes import (
    get_apps_v1,
    init_k8s_client,
    scale_deployment,
)
from ppa.infrastructure.prometheus import (
    PrometheusCircuitBreakerError,
    PrometheusCircuitBreakerTripped,
    get_current_prometheus_url,
    prom_query,
    prom_query_parallel,
    set_prometheus_url,
)

__all__ = [
    # Kubernetes
    "scale_deployment",
    "get_apps_v1",
    "init_k8s_client",
    # Prometheus
    "PrometheusCircuitBreakerError",
    "PrometheusCircuitBreakerTripped",
    "prom_query",
    "prom_query_parallel",
    "set_prometheus_url",
    "get_current_prometheus_url",
]
