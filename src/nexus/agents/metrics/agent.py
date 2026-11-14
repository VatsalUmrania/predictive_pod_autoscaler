"""
NEXUS Metrics Agent Implementation
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import deque

from nexus.agents.base_agent import BaseAgent
from nexus.agents.metrics.checker import check_metric_anomalies
from nexus.agents.metrics.circuit_breaker import PrometheusCircuitBreaker
from nexus.agents.metrics.queries import DEFAULT_QUERIES, execute_promql_query
from nexus.bus.incident_event import (
    AgentType,
    IncidentEvent,
    Severity,
    SignalType,
)
from nexus.bus.nats_client import NATSClient

logger = logging.getLogger(__name__)


class MetricsAgent(BaseAgent):
    """
    Observes Prometheus metrics and emits threshold-breach IncidentEvents.
    """

    QUERIES = DEFAULT_QUERIES

    def __init__(
        self,
        nats_client: NATSClient,
        prometheus_url: str | None = None,
        poll_interval_seconds: float = 30.0,
        http_timeout: float = 5.0,
        cb_failure_threshold: int = 5,
        cb_reset_timeout: float = 60.0,
        namespace: str | None = None,
        deployment_name: str | None = None,
    ):
        super().__init__(
            nats_client=nats_client,
            agent_type=AgentType.METRICS,
            poll_interval_seconds=poll_interval_seconds,
            failure_threshold=cb_failure_threshold,
        )
        url = (
            prometheus_url
            or os.getenv("NEXUS_PROMETHEUS_URL")
            or "http://localhost:9090"
        )
        self.prom_url = url.rstrip("/")
        self.http_timeout = http_timeout
        self.namespace = namespace
        self.deployment_name = deployment_name
        self.cb = PrometheusCircuitBreaker(cb_failure_threshold, cb_reset_timeout)

        self.cpu_threshold = float(os.getenv("NEXUS_CPU_THRESHOLD_PCT", "85.0"))
        self.mem_threshold = float(os.getenv("NEXUS_MEM_THRESHOLD_PCT", "85.0"))
        self.err_threshold = float(os.getenv("NEXUS_ERROR_RATE_THRESHOLD", "0.05"))
        self.lat_threshold_ms = float(os.getenv("NEXUS_LATENCY_P95_MS", "500.0"))
        self.rps_spike_mult = float(os.getenv("NEXUS_RPS_SPIKE_MULTIPLIER", "3.0"))

        self._rps_history: deque = deque(maxlen=20)

    async def _query(self, name: str, promql: str) -> float | None:
        return await execute_promql_query(
            prom_url=self.prom_url,
            name=name,
            promql=promql,
            cb=self.cb,
            timeout=self.http_timeout,
        )

    async def _query_all(self) -> dict[str, float | None]:
        tasks = {
            name: asyncio.create_task(self._query(name, promql))
            for name, promql in self.QUERIES.items()
        }
        return {name: await task for name, task in tasks.items()}

    def _check(self, metrics: dict[str, float | None]) -> list[IncidentEvent]:
        return check_metric_anomalies(
            metrics=metrics,
            namespace=self.namespace,
            deployment_name=self.deployment_name,
            prom_url=self.prom_url,
            cb=self.cb,
            cpu_threshold=self.cpu_threshold,
            mem_threshold=self.mem_threshold,
            err_threshold=self.err_threshold,
            lat_threshold_ms=self.lat_threshold_ms,
            rps_spike_mult=self.rps_spike_mult,
            rps_history=self._rps_history,
        )

    async def on_start(self) -> None:
        pass

    async def on_stop(self) -> None:
        pass

    async def sense(self) -> list[IncidentEvent]:
        metrics = await self._query_all()

        if (
            all(v is None for v in metrics.values())
            and self.cb.state != PrometheusCircuitBreaker.CLOSED
        ):
            return [
                IncidentEvent(
                    agent=AgentType.METRICS,
                    signal_type=SignalType.METRIC_UNAVAILABLE,
                    severity=Severity.CRITICAL,
                    namespace=self.namespace,
                    context={
                        "prometheus_url": self.prom_url,
                        "cb_state": self.cb.state,
                    },
                )
            ]

        return self._check(metrics)
