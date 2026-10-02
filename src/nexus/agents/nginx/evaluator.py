"""
NGINX Endpoint Statistics Evaluator
"""

from __future__ import annotations

from collections import deque

from nexus.bus.incident_event import (
    AgentType,
    IncidentEvent,
    NginxHighErrorContext,
    Severity,
    SignalType,
)
from nexus.telemetry.log_shipper import EndpointStats


def evaluate_endpoint_stats(
    stats_dict: dict[str, EndpointStats],
    rps_history: dict[str, deque],
    error_threshold: float,
    latency_p95_ms: float,
    rps_spike_mult: float,
    min_rps: float,
    window_s: int,
) -> list[IncidentEvent]:
    events: list[IncidentEvent] = []

    for endpoint, stats in list(stats_dict.items()):
        err_rate = stats.error_rate()
        rps = stats.rps()
        p95 = stats.p95_latency()

        if rps < min_rps:
            continue

        if err_rate > error_threshold:
            events.append(
                IncidentEvent(
                    agent=AgentType.NGINX,
                    signal_type=SignalType.HIGH_ERROR_RATE,
                    severity=(
                        Severity.CRITICAL if err_rate > 0.20 else Severity.WARNING
                    ),
                    context=NginxHighErrorContext(
                        endpoint=endpoint,
                        error_rate=err_rate,
                        baseline_rate=0.02,
                        rps=rps,
                        window_seconds=window_s,
                    ).model_dump(),
                    suggested_healing_level=2,
                    confidence=0.88,
                )
            )

        if p95 is not None and (p95 * 1000) > latency_p95_ms:
            events.append(
                IncidentEvent(
                    agent=AgentType.NGINX,
                    signal_type=SignalType.HIGH_LATENCY,
                    severity=Severity.WARNING,
                    context={
                        "endpoint": endpoint,
                        "p95_latency_ms": p95 * 1000,
                        "threshold_ms": latency_p95_ms,
                        "rps": rps,
                    },
                )
            )

        rps_history[endpoint].append(rps)
        history = rps_history[endpoint]
        if len(history) >= 5:
            median = sorted(history)[len(history) // 2]
            if median > 0 and rps > median * rps_spike_mult:
                events.append(
                    IncidentEvent(
                        agent=AgentType.NGINX,
                        signal_type=SignalType.TRAFFIC_SPIKE,
                        severity=Severity.WARNING,
                        context={
                            "endpoint": endpoint,
                            "current_rps": rps,
                            "baseline_rps": median,
                            "spike_multiplier": rps / median,
                            "threshold_mult": rps_spike_mult,
                        },
                        confidence=0.75,
                    )
                )

    return events
