"""
Synthetic DNS and HTTP Network Probers
"""

from __future__ import annotations

import asyncio
import socket
import time

import httpx

from nexus.bus.incident_event import (
    AgentType,
    DNSFailureContext,
    IncidentEvent,
    Severity,
    SignalType,
)


async def probe_dns(hostname: str, timeout_s: float) -> float | None:
    """Resolve hostname via getaddrinfo. Returns latency in ms, or None on failure."""
    loop = asyncio.get_running_loop()
    try:
        start = time.monotonic()
        await asyncio.wait_for(
            loop.run_in_executor(None, socket.getaddrinfo, hostname, None),
            timeout=timeout_s,
        )
        return (time.monotonic() - start) * 1000.0
    except (asyncio.TimeoutError, socket.gaierror, OSError):
        return None


async def check_dns(
    hostname: str,
    timeout_s: float,
    dns_failures: dict[str, int],
) -> IncidentEvent | None:
    """Check DNS resolution and track failure counts."""
    latency = await probe_dns(hostname, timeout_s)
    if latency is None:
        dns_failures[hostname] = dns_failures.get(hostname, 0) + 1
        if dns_failures[hostname] == 1 or dns_failures[hostname] % 3 == 0:
            return IncidentEvent(
                agent=AgentType.NETWORK,
                signal_type=SignalType.DNS_RESOLUTION_FAILURE,
                severity=(
                    Severity.CRITICAL
                    if dns_failures[hostname] >= 3
                    else Severity.WARNING
                ),
                context=DNSFailureContext(
                    hostname=hostname,
                    resolvers_tried=["cluster-dns"],
                    error_message=f"getaddrinfo failed after {timeout_s}s timeout",
                    affected_services=[hostname],
                ).model_dump(),
                suggested_healing_level=1,
                confidence=0.90,
            )
    else:
        dns_failures.pop(hostname, None)
    return None


async def probe_http(url: str, timeout_s: float) -> float | None:
    """HTTP GET to health/ready endpoint. Returns latency in ms, or None on failure."""
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            start = time.monotonic()
            resp = await client.get(url, follow_redirects=True)
            latency_ms = (time.monotonic() - start) * 1000.0
            if resp.status_code >= 500:
                return None
            return latency_ms
    except Exception:
        return None


async def check_service_http(
    hostname: str,
    timeout_s: float,
    latency_threshold_ms: float,
) -> list[IncidentEvent]:
    """Probe common health paths for a service hostname."""
    events: list[IncidentEvent] = []

    for path in ("/healthz", "/health", "/ready", "/"):
        url = f"http://{hostname}{path}"
        latency = await probe_http(url, timeout_s)

        if latency is None:
            events.append(
                IncidentEvent(
                    agent=AgentType.NETWORK,
                    signal_type=SignalType.SERVICE_UNREACHABLE,
                    severity=Severity.WARNING,
                    context={
                        "hostname": hostname,
                        "url": url,
                        "timeout_s": timeout_s,
                    },
                )
            )
            break

        if latency > latency_threshold_ms:
            events.append(
                IncidentEvent(
                    agent=AgentType.NETWORK,
                    signal_type=SignalType.INTER_SERVICE_LATENCY,
                    severity=Severity.WARNING,
                    context={
                        "hostname": hostname,
                        "url": url,
                        "latency_ms": latency,
                        "threshold_ms": latency_threshold_ms,
                    },
                )
            )
        break

    return events
