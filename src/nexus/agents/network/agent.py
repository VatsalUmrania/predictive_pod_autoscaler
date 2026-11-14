"""
NEXUS Network Agent Implementation
"""

from __future__ import annotations

import asyncio
import logging
import os

from nexus.agents.base_agent import BaseAgent
from nexus.agents.network.discovery import discover_k8s_service_hostnames
from nexus.agents.network.probers import check_dns, check_service_http
from nexus.bus.incident_event import AgentType, IncidentEvent
from nexus.bus.nats_client import NATSClient

logger = logging.getLogger(__name__)


class NetworkAgent(BaseAgent):
    """
    Synthetic network prober for DNS and HTTP inter-service connectivity.
    """

    def __init__(
        self,
        nats_client: NATSClient,
        namespaces: list[str] | None = None,
        extra_endpoints: list[str] | None = None,
        dns_timeout_s: float = 3.0,
        http_timeout_s: float = 5.0,
        latency_threshold_ms: float = 500.0,
        refresh_services_every: int = 10,
        poll_interval_seconds: float = 30.0,
    ):
        super().__init__(
            nats_client=nats_client,
            agent_type=AgentType.NETWORK,
            poll_interval_seconds=poll_interval_seconds,
        )
        self.namespaces = namespaces
        self.extra_endpoints = extra_endpoints or []
        self.dns_timeout_s = float(
            os.getenv("NEXUS_NETWORK_DNS_TIMEOUT_S", str(dns_timeout_s))
        )
        self.http_timeout_s = float(
            os.getenv("NEXUS_NETWORK_HTTP_TIMEOUT_S", str(http_timeout_s))
        )
        self.latency_threshold_ms = float(
            os.getenv("NEXUS_NETWORK_LATENCY_THRESHOLD_MS", str(latency_threshold_ms))
        )
        self.refresh_every = refresh_services_every

        self._service_hostnames: list[str] = []
        self._cycle_count = 0
        self._dns_failures: dict[str, int] = {}

    async def _refresh_services(self) -> None:
        loop = asyncio.get_running_loop()
        self._service_hostnames = await loop.run_in_executor(
            None, discover_k8s_service_hostnames, self.namespaces
        )
        logger.info(
            f"[NetworkAgent] Discovered {len(self._service_hostnames)} K8s services"
        )

    async def on_start(self) -> None:
        await self._refresh_services()

    async def on_stop(self) -> None:
        pass

    async def sense(self) -> list[IncidentEvent]:
        self._cycle_count += 1

        if self._cycle_count % self.refresh_every == 0:
            await self._refresh_services()

        all_hosts = list(set(self._service_hostnames))
        events: list[IncidentEvent] = []

        # 1. DNS checks
        dns_results = await asyncio.gather(
            *[check_dns(h, self.dns_timeout_s, self._dns_failures) for h in all_hosts],
            return_exceptions=True,
        )
        for r in dns_results:
            if isinstance(r, IncidentEvent):
                events.append(r)
            elif isinstance(r, Exception):
                logger.debug(f"[NetworkAgent] DNS check exception: {r}")

        # 2. HTTP checks (extra endpoints)
        if self.extra_endpoints:
            http_results = await asyncio.gather(
                *[
                    check_service_http(
                        ep, self.http_timeout_s, self.latency_threshold_ms
                    )
                    for ep in self.extra_endpoints
                ],
                return_exceptions=True,
            )
            for hr in http_results:
                if isinstance(hr, list):
                    events.extend(hr)
                elif isinstance(hr, Exception):
                    logger.debug(f"[NetworkAgent] HTTP check exception: {hr}")

        return events
