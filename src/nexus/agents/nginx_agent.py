"""
NEXUS NGINX Agent
==================
Full NGINX observability agent — extends the Phase 1 LogShipper with
active anomaly detection, upstream health monitoring, and NGINX status API.

Observation sources:
    1. Access log tailing (real-time, via NginxLogShipper)
    2. NGINX stub_status / NGINX Plus API (if available)
    3. Per-endpoint RPS, error rate, and P95 latency from rolling windows

Novel capability (Research gap #6):
    Receives TRAFFIC_SPIKE_PREDICTED signals from DBTrafficCorrelator (Phase 5)
    and pre-emptively adjusts NGINX upstream weights to shift load away from
    pods showing early saturation — acting BEFORE the pod becomes unhealthy.

Phase 2: observes and emits events (pre-emptive shaping added in Phase 5).

Published IncidentEvents:
    HIGH_ERROR_RATE  — endpoint error rate exceeds threshold
    HIGH_LATENCY     — P95 latency exceeds threshold
    UPSTREAM_DOWN    — upstream backend returns no responses
    TRAFFIC_SPIKE    — sudden RPS increase vs rolling baseline
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Dict, List, Optional

import httpx

from nexus.agents.base_agent import BaseAgent
from nexus.bus.incident_event import (
    AgentType,
    IncidentEvent,
    NginxHighErrorContext,
    Severity,
    SignalType,
)
from nexus.bus.nats_client import NATSClient
from nexus.telemetry.log_shipper import EndpointStats, parse_nginx_line

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# NGINX status page parser  (stub_status module)
# ──────────────────────────────────────────────────────────────────────────────

_STUB_STATUS_RE = re.compile(
    r"Active connections:\s*(\d+)\s+"
    r"server accepts handled requests\s+(\d+)\s+(\d+)\s+(\d+)\s+"
    r"Reading:\s*(\d+)\s+Writing:\s*(\d+)\s+Waiting:\s*(\d+)"
)


def parse_stub_status(text: str) -> Optional[Dict]:
    m = _STUB_STATUS_RE.search(text)
    if not m:
        return None
    return {
        "active_connections": int(m.group(1)),
        "accepts":            int(m.group(2)),
        "handled":            int(m.group(3)),
        "requests":           int(m.group(4)),
        "reading":            int(m.group(5)),
        "writing":            int(m.group(6)),
        "waiting":            int(m.group(7)),
    }


# ──────────────────────────────────────────────────────────────────────────────
# NGINX Agent
# ──────────────────────────────────────────────────────────────────────────────

class NginxAgent(BaseAgent):
    """
    Full NGINX observability agent.

    Combines two data sources for a complete picture:
        • Real-time access log parsing (per-endpoint stats)
        • NGINX stub_status page (global connection/request metrics)

    Anomaly thresholds (configurable via env vars):
        NEXUS_NGINX_ERROR_RATE_THRESHOLD  (default: 0.05 = 5%)
        NEXUS_NGINX_LATENCY_P95_MS        (default: 500ms)
        NEXUS_NGINX_RPS_SPIKE_MULT        (default: 3.0×)
        NEXUS_NGINX_MIN_RPS_FOR_ALERTS    (default: 0.1 rps)

    Args:
        log_path:           NGINX access.log path.
        stub_status_url:    NGINX stub_status URL (e.g. http://localhost/nginx_status).
                            Set to None to disable.
        window_seconds:     Rolling window for per-endpoint stats (default 60s).
        poll_interval_seconds: How often to evaluate thresholds (default 15s).
    """

    def __init__(
        self,
        nats_client: NATSClient,
        log_path: str = "/var/log/nginx/access.log",
        stub_status_url: Optional[str] = None,
        window_seconds: int = 60,
        poll_interval_seconds: float = 15.0,
    ):
        super().__init__(
            nats_client           = nats_client,
            agent_type            = AgentType.NGINX,
            poll_interval_seconds = poll_interval_seconds,
        )
        self.log_path        = Path(log_path)
        self.stub_status_url = stub_status_url or os.getenv("NEXUS_NGINX_STUB_STATUS_URL")
        self.window_s        = window_seconds
        self.http_timeout    = 5.0

        # Thresholds
        self.error_threshold = float(os.getenv("NEXUS_NGINX_ERROR_RATE_THRESHOLD", "0.05"))
        self.latency_p95_ms  = float(os.getenv("NEXUS_NGINX_LATENCY_P95_MS",       "500.0"))
        self.rps_spike_mult  = float(os.getenv("NEXUS_NGINX_RPS_SPIKE_MULT",        "3.0"))
        self.min_rps         = float(os.getenv("NEXUS_NGINX_MIN_RPS_FOR_ALERTS",    "0.1"))

        # Per-endpoint rolling stats
        self._stats: Dict[str, EndpointStats] = defaultdict(
            lambda: EndpointStats(window_seconds)
        )
        # Per-endpoint RPS history for spike detection
        self._rps_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=20))

        # Log file state
        self._log_file_pos: int = 0
        self._log_task: Optional[asyncio.Task] = None

    # ── Log tailing ───────────────────────────────────────────────────────────

    async def on_start(self) -> None:
        """Seek to end of log file and start background tail."""
        if self.log_path.exists():
            self._log_file_pos = self.log_path.stat().st_size
        self._log_task = asyncio.create_task(self._tail_log())
        logger.info(f"[NginxAgent] Tailing {self.log_path}")

    async def on_stop(self) -> None:
        if self._log_task:
            self._log_task.cancel()

    async def _tail_log(self) -> None:
        """Background task: continuously reads new log lines and updates stats."""
        while True:
            try:
                if not self.log_path.exists():
                    await asyncio.sleep(5)
                    continue

                with open(self.log_path, "r", errors="replace") as f:
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

    # ── NGINX stub_status ─────────────────────────────────────────────────────

    async def _get_stub_status(self) -> Optional[Dict]:
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

    # ── Threshold evaluation ──────────────────────────────────────────────────

    def _evaluate_stats(self) -> List[IncidentEvent]:
        events: List[IncidentEvent] = []

        for endpoint, stats in list(self._stats.items()):
            err_rate = stats.error_rate()
            rps      = stats.rps()
            p95      = stats.p95_latency()

            if rps < self.min_rps:
                continue   # Not enough traffic to be meaningful

            # Error rate breach
            if err_rate > self.error_threshold:
                events.append(IncidentEvent(
                    agent=AgentType.NGINX,
                    signal_type=SignalType.HIGH_ERROR_RATE,
                    severity=Severity.CRITICAL if err_rate > 0.20 else Severity.WARNING,
                    context=NginxHighErrorContext(
                        endpoint=endpoint,
                        error_rate=err_rate,
                        baseline_rate=0.02,
                        rps=rps,
                        window_seconds=self.window_s,
                    ).model_dump(),
                    suggested_runbook="runbook_high_error_rate_post_deploy_v1",
                    suggested_healing_level=2,
                    confidence=0.88,
                ))

            # P95 latency breach
            if p95 is not None and (p95 * 1000) > self.latency_p95_ms:
                events.append(IncidentEvent(
                    agent=AgentType.NGINX,
                    signal_type=SignalType.HIGH_LATENCY,
                    severity=Severity.WARNING,
                    context={
                        "endpoint":        endpoint,
                        "p95_latency_ms":  p95 * 1000,
                        "threshold_ms":    self.latency_p95_ms,
                        "rps":             rps,
                    },
                ))

            # RPS spike
            self._rps_history[endpoint].append(rps)
            history = self._rps_history[endpoint]
            if len(history) >= 5:
                median = sorted(history)[len(history) // 2]
                if median > 0 and rps > median * self.rps_spike_mult:
                    events.append(IncidentEvent(
                        agent=AgentType.NGINX,
                        signal_type=SignalType.TRAFFIC_SPIKE,
                        severity=Severity.WARNING,
                        context={
                            "endpoint":          endpoint,
                            "current_rps":       rps,
                            "baseline_rps":      median,
                            "spike_multiplier":  rps / median,
                            "threshold_mult":    self.rps_spike_mult,
                        },
                        confidence=0.75,
                    ))

        return events

    # ── BaseAgent interface ───────────────────────────────────────────────────

    async def sense(self) -> List[IncidentEvent]:
        events: List[IncidentEvent] = []

        # Per-endpoint threshold evaluation (from log tail)
        events.extend(self._evaluate_stats())

        # Global NGINX status (stub_status or NGINX Plus API)
        stub = await self._get_stub_status()
        if stub:
            # Upstream "down" heuristic: all connections waiting, none active
            if stub["active_connections"] > 0 and stub["writing"] == 0:
                events.append(IncidentEvent(
                    agent=AgentType.NGINX,
                    signal_type=SignalType.UPSTREAM_DOWN,
                    severity=Severity.CRITICAL,
                    context={
                        "active_connections": stub["active_connections"],
                        "writing":            stub["writing"],
                        "waiting":            stub["waiting"],
                        "note":               "All connections waiting — possible upstream failure",
                    },
                ))

        return events
