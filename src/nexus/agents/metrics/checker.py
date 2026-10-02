"""
Metrics Threshold Checking and Anomaly Event Construction
"""

from __future__ import annotations

from collections import deque

from nexus.agents.metrics.circuit_breaker import PrometheusCircuitBreaker
from nexus.bus.incident_event import (
    AgentType,
    IncidentEvent,
    MetricsAnomalyContext,
    Severity,
    SignalType,
)


def check_metric_anomalies(
    metrics: dict[str, float | None],
    namespace: str | None,
    deployment_name: str | None,
    prom_url: str,
    cb: PrometheusCircuitBreaker,
    cpu_threshold: float,
    mem_threshold: float,
    err_threshold: float,
    lat_threshold_ms: float,
    rps_spike_mult: float,
    rps_history: deque,
) -> list[IncidentEvent]:
    events: list[IncidentEvent] = []

    def _make_anomaly_event(
        metric_name: str,
        current: float,
        threshold: float,
        severity: Severity,
        healing_level: int | None = None,
        confidence: float = 0.85,
    ) -> IncidentEvent:
        score = min(abs(current - threshold) / max(threshold, 1e-6), 1.0)
        return IncidentEvent(
            agent=AgentType.METRICS,
            signal_type=SignalType.THRESHOLD_BREACH,
            severity=severity,
            namespace=namespace,
            resource_name=deployment_name,
            context=MetricsAnomalyContext(
                metric_name=metric_name,
                current_value=current,
                threshold=threshold,
                anomaly_score=score,
                window_seconds=120,
            ).model_dump(),
            suggested_healing_level=healing_level,
            confidence=confidence,
        )

    cpu = metrics.get("cpu_utilization_pct")
    if cpu is not None and cpu > cpu_threshold:
        events.append(
            _make_anomaly_event(
                "cpu_utilization_pct",
                cpu,
                cpu_threshold,
                severity=Severity.CRITICAL if cpu > 95 else Severity.WARNING,
            )
        )

    mem = metrics.get("memory_utilization_pct")
    if mem is not None and mem > mem_threshold:
        events.append(
            _make_anomaly_event(
                "memory_utilization_pct",
                mem,
                mem_threshold,
                severity=Severity.CRITICAL if mem > 95 else Severity.WARNING,
                healing_level=1,
            )
        )

    err = metrics.get("error_rate")
    if err is not None and err > err_threshold:
        events.append(
            _make_anomaly_event(
                "error_rate",
                err,
                err_threshold,
                severity=Severity.CRITICAL if err > 0.20 else Severity.WARNING,
                healing_level=2,
                confidence=0.88,
            )
        )

    lat = metrics.get("latency_p95_ms")
    if lat is not None and lat > lat_threshold_ms:
        events.append(
            _make_anomaly_event(
                "latency_p95_ms",
                lat,
                lat_threshold_ms,
                severity=Severity.WARNING,
                confidence=0.75,
            )
        )

    rps = metrics.get("rps")
    if rps is not None:
        rps_history.append(rps)
        if len(rps_history) >= 5:
            median = sorted(rps_history)[len(rps_history) // 2]
            spike_threshold = median * rps_spike_mult
            if median > 0 and rps > spike_threshold:
                events.append(
                    _make_anomaly_event(
                        "rps_spike",
                        rps,
                        spike_threshold,
                        severity=Severity.WARNING,
                        confidence=0.70,
                    )
                )

    if cb.state == PrometheusCircuitBreaker.OPEN:
        events.append(
            IncidentEvent(
                agent=AgentType.METRICS,
                signal_type=SignalType.CIRCUIT_BREAKER_TRIPPED,
                severity=Severity.CRITICAL,
                context={
                    "consecutive_failures": cb._failures,
                    "prometheus_url": prom_url,
                    "message": "Prometheus unreachable — metrics suspended",
                },
            )
        )

    return events
