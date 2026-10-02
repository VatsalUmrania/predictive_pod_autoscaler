"""
NEXUS NGINX Agent Implementation
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict, deque
from pathlib import Path

import httpx

from nexus.agents.base_agent import BaseAgent
from nexus.agents.nginx.evaluator import evaluate_endpoint_stats
from nexus.agents.nginx.parser import parse_stub_status
from nexus.bus.incident_event import (
    AgentType,
    IncidentEvent,
    Severity,
    SignalType,
)
from nexus.bus.nats_client import NATSClient
from nexus.telemetry.log_shipper import EndpointStats, parse_nginx_line

logger = logging.getLogger(__name__)


class NginxAgent(BaseAgent):
    """
    Full NGINX observability agent.
    """

    def __init__(
        self,
        nats_client: NATSClient,
        log_path: str = "/var/log/nginx/access.log",
        stub_status_url: str | None = None,
        window_seconds: int = 60,
        poll_interval_seconds: float = 15.0,
    ):
        super().__init__(
            nats_client=nats_client,
            agent_type=AgentType.NGINX,
            poll_interval_seconds=poll_interval_seconds,
        )
        self.log_path = Path(log_path)
        self.stub_status_url = stub_status_url or os.getenv(
            "NEXUS_NGINX_STUB_STATUS_URL"
        )
        self.window_s = window_seconds
        self.http_timeout = 5.0

        self.error_threshold = float(
            os.getenv("NEXUS_NGINX_ERROR_RATE_THRESHOLD", "0.05")
        )
        self.latency_p95_ms = float(os.getenv("NEXUS_NGINX_LATENCY_P95_MS", "500.0"))
        self.rps_spike_mult = float(os.getenv("NEXUS_NGINX_RPS_SPIKE_MULT", "3.0"))
        self.min_rps = float(os.getenv("NEXUS_NGINX_MIN_RPS_FOR_ALERTS", "0.1"))

        self._stats: dict[str, EndpointStats] = defaultdict(
            lambda: EndpointStats(window_seconds)
        )
        self._rps_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=20))
        self._log_file_pos: int = 0
        self._log_task: asyncio.Task | None = None

    async def on_start(self) -> None:
        if self.log_path.exists():
            self._log_file_pos = self.log_path.stat().st_size
        self._log_task = asyncio.create_task(self._tail_log())
        logger.info(f"[NginxAgent] Tailing {self.log_path}")

    async def on_stop(self) -> None:
        if self._log_task:
            self._log_task.cancel()

    async def _tail_log(self) -> None:
        while True:
            try:
                if not self.log_path.exists():
                    await asyncio.sleep(5)
                    continue

                with open(self.log_path, errors="replace") as f:
                    f.seek(self._log_file_pos)
                    while True:
                        line = f.readline()
                        if not line:
                            self._log_file_pos = f.tell()
                            await asyncio.sleep(0.1)
                            break
                        record = parse_nginx_line(line)
                        if record:
                            endpoint = record["path"].split("?")[0]
                            self._stats[endpoint].record(
                                record["status"], record.get("request_time")
                            )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"[NginxAgent] Log tail error: {exc}")
                await asyncio.sleep(5)

    async def _get_stub_status(self) -> dict | None:
        if not self.stub_status_url:
            return None
        try:
            async with httpx.AsyncClient(timeout=self.http_timeout) as client:
                resp = await client.get(self.stub_status_url)
                if resp.status_code == 200:
                    return parse_stub_status(resp.text)
        except Exception:
            pass
        return None

    def _evaluate_stats(self) -> list[IncidentEvent]:
        return evaluate_endpoint_stats(
            stats_dict=self._stats,
            rps_history=self._rps_history,
            error_threshold=self.error_threshold,
            latency_p95_ms=self.latency_p95_ms,
            rps_spike_mult=self.rps_spike_mult,
            min_rps=self.min_rps,
            window_s=self.window_s,
        )

    async def sense(self) -> list[IncidentEvent]:
        events: list[IncidentEvent] = []
        events.extend(self._evaluate_stats())

        stub = await self._get_stub_status()
        if stub:
            if stub["active_connections"] > 0 and stub["writing"] == 0:
                events.append(
                    IncidentEvent(
                        agent=AgentType.NGINX,
                        signal_type=SignalType.UPSTREAM_DOWN,
                        severity=Severity.CRITICAL,
                        context={
                            "active_connections": stub["active_connections"],
                            "writing": stub["writing"],
                            "waiting": stub["waiting"],
                            "note": "All connections waiting — possible upstream failure",
                        },
                    )
                )

        return events
