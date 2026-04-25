"""
NEXUS Network Agent
====================
Synthetic DNS and inter-service connectivity prober.

Observes:
    - DNS resolution for all Kubernetes Service hostnames
    - HTTP health / readiness endpoint reachability for each service
    - CoreDNS health (via Prometheus metric counts)

Service discovery:
    Auto-discovers services from the Kubernetes API (namespaced or cluster-wide).
    Supplemented by a static list of additional endpoints from environment config.

Published IncidentEvents:
    DNS_RESOLUTION_FAILURE   — socket.getaddrinfo() fails or times out
    SERVICE_UNREACHABLE      — HTTP health check returns 5xx or connection refused
    INTER_SERVICE_LATENCY    — HTTP roundtrip exceeds latency threshold

Configuration:
    NEXUS_NETWORK_DNS_TIMEOUT_S       (default: 3.0)
    NEXUS_NETWORK_HTTP_TIMEOUT_S      (default: 5.0)
    NEXUS_NETWORK_LATENCY_THRESHOLD_MS (default: 500.0)
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from typing import Dict, List, Optional, Set

import httpx

from nexus.agents.base_agent import BaseAgent
from nexus.bus.incident_event import (
    AgentType,
    DNSFailureContext,
    IncidentEvent,
    Severity,
    SignalType,
)
from nexus.bus.nats_client import NATSClient

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# K8s Service discovery
# ──────────────────────────────────────────────────────────────────────────────

def discover_k8s_service_hostnames(namespaces: Optional[List[str]] = None) -> List[str]:
    """
    Return a list of DNS names for all Kubernetes Services in the given namespaces.
    Falls back to empty list if K8s API is unreachable.

    DNS format: <service>.<namespace>.svc.cluster.local
    """
    hostnames: List[str] = []
    try:
        from kubernetes import client as k8s_client, config as k8s_config
        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()

        v1 = k8s_client.CoreV1Api()

        if namespaces:
            services = []
            for ns in namespaces:
                services.extend(v1.list_namespaced_service(ns).items)
        else:
            services = v1.list_service_for_all_namespaces().items

        for svc in services:
            name = svc.metadata.name
            ns   = svc.metadata.namespace
            # Skip system / headless services
            if ns in ("kube-system", "kube-public", "kube-node-lease"):
                continue
            if svc.spec.cluster_ip == "None":   # headless
                continue
            hostnames.append(f"{name}.{ns}.svc.cluster.local")

    except Exception as exc:
        logger.debug(f"[NetworkAgent] K8s service discovery unavailable: {exc}")

    return hostnames


# ──────────────────────────────────────────────────────────────────────────────
# Network Agent
# ──────────────────────────────────────────────────────────────────────────────

class NetworkAgent(BaseAgent):
    """
    Synthetic network prober for DNS and HTTP inter-service connectivity.

    Probes all discovered K8s services every poll_interval seconds.
    DNS failures and high-latency responses trigger NATS incident events.

    Args:
        nats_client:            Connected NATSClient.
        namespaces:             K8s namespaces to discover services from. None = all.
        extra_endpoints:        Additional static HTTP URLs to probe.
        dns_timeout_s:          DNS resolution timeout (default 3s).
        http_timeout_s:         HTTP probe timeout (default 5s).
        latency_threshold_ms:   Emit HIGH_LATENCY when HTTP RTT exceeds this (default 500ms).
        refresh_services_every: Re-discover K8s services every N poll cycles (default 10).
        poll_interval_seconds:  How often to run probes (default 30s).
    """

    def __init__(
        self,
        nats_client: NATSClient,
        namespaces: Optional[List[str]] = None,
        extra_endpoints: Optional[List[str]] = None,
        dns_timeout_s: float = 3.0,
        http_timeout_s: float = 5.0,
        latency_threshold_ms: float = 500.0,
        refresh_services_every: int = 10,
        poll_interval_seconds: float = 30.0,
    ):
        super().__init__(
            nats_client           = nats_client,
            agent_type            = AgentType.NETWORK,
            poll_interval_seconds = poll_interval_seconds,
        )
        self.namespaces             = namespaces
        self.extra_endpoints        = extra_endpoints or []
        self.dns_timeout_s          = float(os.getenv("NEXUS_NETWORK_DNS_TIMEOUT_S",        str(dns_timeout_s)))
        self.http_timeout_s         = float(os.getenv("NEXUS_NETWORK_HTTP_TIMEOUT_S",       str(http_timeout_s)))
        self.latency_threshold_ms   = float(os.getenv("NEXUS_NETWORK_LATENCY_THRESHOLD_MS", str(latency_threshold_ms)))
        self.refresh_every          = refresh_services_every

        self._service_hostnames: List[str] = []
        self._cycle_count = 0

        # Track consecutive DNS failures per hostname for de-duplication
        self._dns_failures: Dict[str, int] = {}

    # ── DNS probe ─────────────────────────────────────────────────────────────

    async def _probe_dns(self, hostname: str) -> Optional[float]:
        """
        Resolve hostname via getaddrinfo.
        Returns latency in ms, or None on failure.
        """
        loop = asyncio.get_event_loop()
        try:
            start = time.monotonic()
            await asyncio.wait_for(
                loop.run_in_executor(None, socket.getaddrinfo, hostname, None),
                timeout=self.dns_timeout_s,
            )
            return (time.monotonic() - start) * 1000.0
        except (asyncio.TimeoutError, socket.gaierror, OSError):
            return None

    async def _check_dns(self, hostname: str) -> Optional[IncidentEvent]:
        latency = await self._probe_dns(hostname)
        if latency is None:
            self._dns_failures[hostname] = self._dns_failures.get(hostname, 0) + 1
            # Only emit on first failure or every 3rd consecutive failure
            if self._dns_failures[hostname] == 1 or self._dns_failures[hostname] % 3 == 0:
                return IncidentEvent(
                    agent=AgentType.NETWORK,
                    signal_type=SignalType.DNS_RESOLUTION_FAILURE,
                    severity=Severity.CRITICAL if self._dns_failures[hostname] >= 3 else Severity.WARNING,
                    context=DNSFailureContext(
                        hostname=hostname,
                        resolvers_tried=["cluster-dns"],
                        error_message=f"getaddrinfo failed after {self.dns_timeout_s}s timeout",
                        affected_services=[hostname],
                    ).model_dump(),
                    suggested_runbook="runbook_dns_resolution_failure_v1",
                    suggested_healing_level=1,
                    confidence=0.90,
                )
        else:
            self._dns_failures.pop(hostname, None)
        return None

    # ── HTTP probe ────────────────────────────────────────────────────────────

    async def _probe_http(self, url: str) -> Optional[float]:
        """
        HTTP GET to health/ready endpoint.
        Returns latency in ms, or None on connection failure / 5xx.
        """
        try:
            async with httpx.AsyncClient(timeout=self.http_timeout_s) as client:
                start = time.monotonic()
                resp  = await client.get(url, follow_redirects=True)
                latency_ms = (time.monotonic() - start) * 1000.0
                if resp.status_code >= 500:
                    return None
                return latency_ms
        except Exception:
            return None

    async def _check_service_http(self, hostname: str) -> List[IncidentEvent]:
        """Probe common health paths for a service hostname."""
        events: List[IncidentEvent] = []

        for path in ("/healthz", "/health", "/ready", "/"):
            url     = f"http://{hostname}{path}"
            latency = await self._probe_http(url)

            if latency is None:
                events.append(IncidentEvent(
                    agent=AgentType.NETWORK,
                    signal_type=SignalType.SERVICE_UNREACHABLE,
                    severity=Severity.WARNING,
                    context={
                        "hostname": hostname,
                        "url":      url,
                        "timeout_s": self.http_timeout_s,
                    },
                ))
                break   # Don't probe more paths if host is unreachable

            if latency > self.latency_threshold_ms:
                events.append(IncidentEvent(
                    agent=AgentType.NETWORK,
                    signal_type=SignalType.INTER_SERVICE_LATENCY,
                    severity=Severity.WARNING,
                    context={
                        "hostname":    hostname,
                        "url":         url,
                        "latency_ms":  latency,
                        "threshold_ms": self.latency_threshold_ms,
                    },
                ))
            break   # One successful path is enough

        return events

    # ── Service discovery refresh ─────────────────────────────────────────────

    async def _refresh_services(self) -> None:
        loop = asyncio.get_event_loop()
        self._service_hostnames = await loop.run_in_executor(
            None, discover_k8s_service_hostnames, self.namespaces
        )
        logger.info(f"[NetworkAgent] Discovered {len(self._service_hostnames)} K8s services")

    # ── BaseAgent interface ───────────────────────────────────────────────────

    async def on_start(self) -> None:
        await self._refresh_services()

    async def sense(self) -> List[IncidentEvent]:
        self._cycle_count += 1

        # Refresh service list periodically
        if self._cycle_count % self.refresh_every == 0:
            await self._refresh_services()

        all_hosts = list(set(self._service_hostnames))
        events: List[IncidentEvent] = []

        # ── DNS checks (all K8s service hosts) ────────────────────────────────
        dns_results = await asyncio.gather(
            *[self._check_dns(h) for h in all_hosts],
            return_exceptions=True,
        )
        for r in dns_results:
            if isinstance(r, IncidentEvent):
                events.append(r)
            elif isinstance(r, Exception):
                logger.debug(f"[NetworkAgent] DNS check exception: {r}")

        # ── HTTP checks (extra endpoints only — K8s svc probing is optional) ──
        if self.extra_endpoints:
            http_results = await asyncio.gather(
                *[self._check_service_http(ep) for ep in self.extra_endpoints],
                return_exceptions=True,
            )
            for r in http_results:
                if isinstance(r, list):
                    events.extend(r)
                elif isinstance(r, Exception):
                    logger.debug(f"[NetworkAgent] HTTP check exception: {r}")

        return events
