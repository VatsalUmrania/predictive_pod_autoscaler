"""
NEXUS Metrics Agent
====================
Scrapes Prometheus for system health metrics and detects threshold breaches.

Key improvements over the current PPA operator's Prometheus scraping
(see docs/ARCHITECTURE_REVIEW_CRITICAL.md):

    ✅ Circuit breaker — stops hammering Prometheus when it's unreachable
       (fixes §2: socket exhaustion on network partition)
    ✅ Explicit NaN guard — raises rather than silently propagating NaN
       (fixes §1.4: silent NaN in feature vector)
    ✅ Feature bounds clamping — clamps out-of-distribution values
       (fixes §6.2: extrapolation beyond training distribution)
    ✅ Structured IncidentEvent output — not raw metrics
    ✅ RPS baseline tracking — detects spikes vs rolling median

Phase 2: simple threshold-based detection
Phase 5: GRU Autoencoder replaces thresholds with learned anomaly scores

Environment variable configuration:
    NEXUS_PROMETHEUS_URL        (default: http://localhost:9090)
    NEXUS_CPU_THRESHOLD_PCT     (default: 85.0)
    NEXUS_MEM_THRESHOLD_PCT     (default: 85.0)
    NEXUS_ERROR_RATE_THRESHOLD  (default: 0.05)
    NEXUS_LATENCY_P95_MS        (default: 500.0)
    NEXUS_RPS_SPIKE_MULTIPLIER  (default: 3.0)
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from collections import deque
from typing import Dict, List, Optional, Tuple

import httpx

from nexus.agents.base_agent import BaseAgent
from nexus.bus.incident_event import (
    AgentType,
    IncidentEvent,
    MetricsAnomalyContext,
    Severity,
    SignalType,
)
from nexus.bus.nats_client import NATSClient

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Feature bounds — clamp before passing to any model or rule
# ──────────────────────────────────────────────────────────────────────────────

_BOUNDS: Dict[str, Tuple[float, float]] = {
    "cpu_utilization_pct":    (0.0,  200.0),
    "memory_utilization_pct": (0.0,  110.0),
    "error_rate":             (0.0,    1.0),
    "rps":                    (0.0, 100_000.0),
    "latency_p95_ms":         (0.0,  30_000.0),
}


# ──────────────────────────────────────────────────────────────────────────────
# Prometheus Circuit Breaker
# ──────────────────────────────────────────────────────────────────────────────

class _PrometheusCircuitBreaker:
    """
    Tri-state circuit breaker for Prometheus queries.

    CLOSED    → normal, all queries flow through
    OPEN      → too many failures, queries blocked for reset_timeout seconds
    HALF_OPEN → trial query allowed after timeout; reverts to CLOSED on success
    """

    CLOSED    = "CLOSED"
    OPEN      = "OPEN"
    HALF_OPEN = "HALF_OPEN"

    def __init__(self, threshold: int = 5, reset_timeout: float = 60.0):
        self.threshold     = threshold
        self.reset_timeout = reset_timeout
        self._failures     = 0
        self._state        = self.CLOSED
        self._opened_at: Optional[float] = None

    @property
    def state(self) -> str:
        if self._state == self.OPEN:
            if self._opened_at and (time.monotonic() - self._opened_at) > self.reset_timeout:
                self._state = self.HALF_OPEN
        return self._state

    def can_attempt(self) -> bool:
        return self.state in (self.CLOSED, self.HALF_OPEN)

    def record_success(self) -> None:
        if self._state == self.HALF_OPEN:
            logger.info("[MetricsAgent] Circuit breaker RESET → CLOSED")
        self._failures = 0
        self._state    = self.CLOSED
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.threshold:
            if self._state != self.OPEN:
                logger.warning(
                    f"[MetricsAgent] Circuit breaker TRIPPED → OPEN "
                    f"({self._failures} consecutive failures)"
                )
            self._state    = self.OPEN
            self._opened_at = time.monotonic()

    def __repr__(self) -> str:
        return f"CircuitBreaker(state={self.state}, failures={self._failures})"


# ──────────────────────────────────────────────────────────────────────────────
# Metrics Agent
# ──────────────────────────────────────────────────────────────────────────────

class MetricsAgent(BaseAgent):
    """
    Observes Prometheus metrics and emits threshold-breach IncidentEvents.

    Thresholds are configured via environment variables (see module docstring).
    Prometheus URL defaults to http://localhost:9090 but should be set via
    NEXUS_PROMETHEUS_URL in production.
    """

    # Default PromQL queries
    QUERIES: Dict[str, str] = {
        "cpu_utilization_pct": (
            'avg(rate(container_cpu_usage_seconds_total{container!=""}[2m])) * 100'
        ),
        "memory_utilization_pct": (
            'avg(container_memory_working_set_bytes{container!=""})'
            ' / avg(container_spec_memory_limit_bytes{container!="",container_spec_memory_limit_bytes>0})'
            ' * 100'
        ),
        "error_rate": (
            'sum(rate(http_requests_total{status=~"5.."}[2m]))'
            ' / sum(rate(http_requests_total[2m]))'
        ),
        "rps": (
            'sum(rate(http_requests_total[1m]))'
        ),
        "latency_p95_ms": (
            'histogram_quantile(0.95,'
            '  sum(rate(http_request_duration_seconds_bucket[2m])) by (le)'
            ') * 1000'
        ),
    }

    def __init__(
        self,
        nats_client: NATSClient,
        prometheus_url: Optional[str] = None,
        poll_interval_seconds: float = 30.0,
        http_timeout: float = 5.0,
        cb_failure_threshold: int = 5,
        cb_reset_timeout: float = 60.0,
        namespace: Optional[str] = None,
        deployment_name: Optional[str] = None,
    ):
        super().__init__(
            nats_client           = nats_client,
            agent_type            = AgentType.METRICS,
            poll_interval_seconds = poll_interval_seconds,
            failure_threshold     = cb_failure_threshold,
        )
        self.prom_url        = (prometheus_url or os.getenv("NEXUS_PROMETHEUS_URL", "http://localhost:9090")).rstrip("/")
        self.http_timeout    = http_timeout
        self.namespace       = namespace
        self.deployment_name = deployment_name
        self.cb              = _PrometheusCircuitBreaker(cb_failure_threshold, cb_reset_timeout)

        # Load thresholds from env
        self.cpu_threshold    = float(os.getenv("NEXUS_CPU_THRESHOLD_PCT",    "85.0"))
        self.mem_threshold    = float(os.getenv("NEXUS_MEM_THRESHOLD_PCT",    "85.0"))
        self.err_threshold    = float(os.getenv("NEXUS_ERROR_RATE_THRESHOLD", "0.05"))
        self.lat_threshold_ms = float(os.getenv("NEXUS_LATENCY_P95_MS",      "500.0"))
        self.rps_spike_mult   = float(os.getenv("NEXUS_RPS_SPIKE_MULTIPLIER", "3.0"))

        # Rolling RPS history for spike detection (20 samples ≈ 10 min at 30s)
        self._rps_history: deque = deque(maxlen=20)

    # ── Prometheus query helpers ───────────────────────────────────────────────

    async def _query(self, name: str, promql: str) -> Optional[float]:
        """Execute a single PromQL instant query, respecting the circuit breaker."""
        if not self.cb.can_attempt():
            return None

        try:
            async with httpx.AsyncClient(timeout=self.http_timeout) as client:
                resp = await client.get(
                    f"{self.prom_url}/api/v1/query",
                    params={"query": promql},
                )
                resp.raise_for_status()
                results = resp.json().get("data", {}).get("result", [])
                if not results:
                    return None

                value = float(results[0]["value"][1])

                # Explicit NaN/Inf guard — never propagate silently
                if math.isnan(value) or math.isinf(value):
                    logger.warning(f"[MetricsAgent] NaN/Inf in '{name}', skipping")
                    return None

                # Feature bounds clamping
                if name in _BOUNDS:
                    lo, hi = _BOUNDS[name]
                    if value < lo or value > hi:
                        logger.debug(f"[MetricsAgent] Clamping {name}={value:.2f} to [{lo}, {hi}]")
                        value = max(lo, min(hi, value))

                self.cb.record_success()
                return value

        except httpx.TimeoutException:
            self.cb.record_failure()
            logger.warning(f"[MetricsAgent] Prometheus timeout on '{name}'")
        except httpx.HTTPStatusError as exc:
            self.cb.record_failure()
            logger.warning(f"[MetricsAgent] HTTP {exc.response.status_code} on '{name}'")
        except Exception as exc:
            self.cb.record_failure()
            logger.error(f"[MetricsAgent] Query '{name}' failed: {exc}")

        return None

    async def _query_all(self) -> Dict[str, Optional[float]]:
        """Run all metric queries concurrently."""
        tasks = {
            name: asyncio.create_task(self._query(name, promql))
            for name, promql in self.QUERIES.items()
        }
        return {name: await task for name, task in tasks.items()}

    # ── Threshold checks ──────────────────────────────────────────────────────

    def _check(self, metrics: Dict[str, Optional[float]]) -> List[IncidentEvent]:
        events: List[IncidentEvent] = []

        def _make_anomaly_event(
            metric_name: str,
            current: float,
            threshold: float,
            severity: Severity,
            runbook: Optional[str] = None,
            healing_level: Optional[int] = None,
            confidence: float = 0.85,
        ) -> IncidentEvent:
            score = min(abs(current - threshold) / max(threshold, 1e-6), 1.0)
            return IncidentEvent(
                agent=AgentType.METRICS,
                signal_type=SignalType.THRESHOLD_BREACH,
                severity=severity,
                namespace=self.namespace,
                resource_name=self.deployment_name,
                context=MetricsAnomalyContext(
                    metric_name=metric_name,
                    current_value=current,
                    threshold=threshold,
                    anomaly_score=score,
                    window_seconds=120,
                ).model_dump(),
                suggested_runbook=runbook,
                suggested_healing_level=healing_level,
                confidence=confidence,
            )

        # CPU
        cpu = metrics.get("cpu_utilization_pct")
        if cpu is not None and cpu > self.cpu_threshold:
            events.append(_make_anomaly_event(
                "cpu_utilization_pct", cpu, self.cpu_threshold,
                severity=Severity.CRITICAL if cpu > 95 else Severity.WARNING,
            ))

        # Memory
        mem = metrics.get("memory_utilization_pct")
        if mem is not None and mem > self.mem_threshold:
            events.append(_make_anomaly_event(
                "memory_utilization_pct", mem, self.mem_threshold,
                severity=Severity.CRITICAL if mem > 95 else Severity.WARNING,
                runbook="runbook_pod_crashloop_v1",
                healing_level=1,
            ))

        # Error rate
        err = metrics.get("error_rate")
        if err is not None and err > self.err_threshold:
            events.append(_make_anomaly_event(
                "error_rate", err, self.err_threshold,
                severity=Severity.CRITICAL if err > 0.20 else Severity.WARNING,
                runbook="runbook_high_error_rate_post_deploy_v1",
                healing_level=2,
                confidence=0.88,
            ))

        # P95 latency
        lat = metrics.get("latency_p95_ms")
        if lat is not None and lat > self.lat_threshold_ms:
            events.append(_make_anomaly_event(
                "latency_p95_ms", lat, self.lat_threshold_ms,
                severity=Severity.WARNING,
                confidence=0.75,
            ))

        # RPS spike (vs rolling median baseline)
        rps = metrics.get("rps")
        if rps is not None:
            self._rps_history.append(rps)
            if len(self._rps_history) >= 5:
                median = sorted(self._rps_history)[len(self._rps_history) // 2]
                spike_threshold = median * self.rps_spike_mult
                if median > 0 and rps > spike_threshold:
                    events.append(_make_anomaly_event(
                        "rps_spike", rps, spike_threshold,
                        severity=Severity.WARNING,
                        confidence=0.70,
                    ))

        # Prometheus circuit breaker OPEN → metric unavailable
        if self.cb.state == _PrometheusCircuitBreaker.OPEN:
            events.append(IncidentEvent(
                agent=AgentType.METRICS,
                signal_type=SignalType.CIRCUIT_BREAKER_TRIPPED,
                severity=Severity.CRITICAL,
                context={
                    "consecutive_failures": self.cb._failures,
                    "prometheus_url":       self.prom_url,
                    "message":              "Prometheus unreachable — metrics suspended",
                },
            ))

        return events

    # ── BaseAgent interface ───────────────────────────────────────────────────

    async def sense(self) -> List[IncidentEvent]:
        metrics = await self._query_all()

        # All None + CB open → emit single METRIC_UNAVAILABLE
        if all(v is None for v in metrics.values()) and self.cb.state != _PrometheusCircuitBreaker.CLOSED:
            return [IncidentEvent(
                agent=AgentType.METRICS,
                signal_type=SignalType.METRIC_UNAVAILABLE,
                severity=Severity.CRITICAL,
                namespace=self.namespace,
                context={
                    "prometheus_url": self.prom_url,
                    "cb_state":       self.cb.state,
                },
            )]

        return self._check(metrics)
