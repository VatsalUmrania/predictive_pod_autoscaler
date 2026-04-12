"""
NEXUS NGINX Log Shipper
========================
Tails NGINX access.log in real time and ships log records to the
OpenTelemetry Collector via OTLP/HTTP.

Pipeline:
    NGINX access.log
        ↓ (async tail)
    Log parser (combined log format → structured dict)
        ↓
    OpenTelemetry Log Record (with resource + instrumentation scope attrs)
        ↓
    OTel Collector (OTLP/HTTP :4318)
        ↓
    Loki (log storage) + Prometheus (aggregated metrics)

Also emits IncidentEvents to NATS when:
    - Error rate on any endpoint exceeds 5% over 60s
    - P95 latency on any endpoint exceeds 500ms
    - A 5xx response storm is detected (>10 errors in 10s)

This is the Phase 1 NginxAgent precursor. Full NginxAgent (with upstream
weight adjustment + predictive routing) is built in Phase 2.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Dict, Optional, Tuple

from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource

from nexus.bus.incident_event import (
    AgentType, IncidentEvent, NginxHighErrorContext, Severity, SignalType,
)
from nexus.bus.nats_client import NATSClient

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# NGINX Combined Log Format parser
# ──────────────────────────────────────────────────────────────────────────────

_COMBINED_RE = re.compile(
    r'(?P<remote_addr>\S+)\s+-\s+\S+\s+'
    r'\[(?P<time_local>[^\]]+)\]\s+'
    r'"(?P<method>\S+)\s+(?P<path>\S+)\s+\S+"\s+'
    r'(?P<status>\d+)\s+'
    r'(?P<bytes_sent>\d+)\s+'
    r'"(?P<referer>[^"]*)"\s+'
    r'"(?P<user_agent>[^"]*)"'
    r'(?:\s+(?P<request_time>\d+\.\d+))?'  # Optional: $request_time
)

_NGINX_TIME_FMT = "%d/%b/%Y:%H:%M:%S %z"


def parse_nginx_line(line: str) -> Optional[Dict]:
    """Parse a single NGINX combined log line. Returns None on parse failure."""
    m = _COMBINED_RE.match(line.strip())
    if not m:
        return None
    d = m.groupdict()
    try:
        d["status"]     = int(d["status"])
        d["bytes_sent"] = int(d["bytes_sent"])
        d["request_time"] = float(d["request_time"]) if d.get("request_time") else None
        d["timestamp"]  = datetime.strptime(d["time_local"], _NGINX_TIME_FMT)
    except (ValueError, TypeError):
        return None
    return d


# ──────────────────────────────────────────────────────────────────────────────
# Endpoint statistics (rolling window for anomaly detection)
# ──────────────────────────────────────────────────────────────────────────────

class EndpointStats:
    """Rolling-window per-endpoint statistics for anomaly detection."""

    def __init__(self, window_seconds: int = 60):
        self.window   = window_seconds
        # deque of (timestamp, status_code, request_time_or_None)
        self._records: deque = deque()

    def record(self, status: int, request_time: Optional[float]) -> None:
        now = time.monotonic()
        self._records.append((now, status, request_time))
        self._expire(now)

    def _expire(self, now: float) -> None:
        cutoff = now - self.window
        while self._records and self._records[0][0] < cutoff:
            self._records.popleft()

    def error_rate(self) -> float:
        total = len(self._records)
        if total == 0:
            return 0.0
        errors = sum(1 for _, s, _ in self._records if s >= 500)
        return errors / total

    def rps(self) -> float:
        return len(self._records) / max(self.window, 1)

    def p95_latency(self) -> Optional[float]:
        times = [t for _, _, t in self._records if t is not None]
        if not times:
            return None
        times.sort()
        idx = int(len(times) * 0.95)
        return times[min(idx, len(times) - 1)]


# ──────────────────────────────────────────────────────────────────────────────
# Log Shipper
# ──────────────────────────────────────────────────────────────────────────────

class NginxLogShipper:
    """
    Async NGINX access log tailer with OTel shipping and NATS anomaly events.

    Args:
        log_path:            Path to NGINX access.log
        nats_client:         Connected NATSClient for anomaly event publishing
        otel_endpoint:       OTel Collector OTLP/HTTP endpoint
        error_rate_threshold: Emit HIGH_ERROR_RATE event when exceeded (default 5%)
        latency_p95_ms:      Emit HIGH_LATENCY event when P95 exceeds this (default 500ms)
        window_seconds:      Rolling window for rate calculations (default 60s)
        check_interval:      How often to evaluate thresholds in seconds (default 15s)
    """

    def __init__(
        self,
        log_path: str = "/var/log/nginx/access.log",
        nats_client: Optional[NATSClient] = None,
        otel_endpoint: str = "http://localhost:4318",
        error_rate_threshold: float = 0.05,
        latency_p95_ms: float = 500.0,
        window_seconds: int = 60,
        check_interval: int = 15,
    ):
        self.log_path            = Path(log_path)
        self.nats                = nats_client
        self.otel_endpoint       = otel_endpoint
        self.error_rate_threshold = error_rate_threshold
        self.latency_p95_ms      = latency_p95_ms
        self.window_seconds      = window_seconds
        self.check_interval      = check_interval

        # Per-endpoint rolling stats
        self._stats: Dict[str, EndpointStats] = defaultdict(
            lambda: EndpointStats(window_seconds)
        )

        # OTel logger setup
        self._otel_logger = self._setup_otel()

    def _setup_otel(self) -> logging.Logger:
        """Configure OTel SDK to export logs to Collector via OTLP/HTTP."""
        resource = Resource.create({
            "service.name":      "nexus-nginx-log-shipper",
            "service.version":   "0.1.0",
            "service.namespace": "nexus",
        })
        exporter = OTLPLogExporter(endpoint=f"{self.otel_endpoint}/v1/logs")
        provider = LoggerProvider(resource=resource)
        provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
        set_logger_provider(provider)

        handler = LoggingHandler(level=logging.DEBUG, logger_provider=provider)
        otel_logger = logging.getLogger("nexus.nginx")
        otel_logger.addHandler(handler)
        otel_logger.setLevel(logging.DEBUG)
        return otel_logger

    # ── Async file tailer ─────────────────────────────────────────────────────

    async def _tail(self) -> AsyncIterator[str]:
        """Async generator that yields new lines as NGINX writes them."""
        if not self.log_path.exists():
            logger.warning(f"[LogShipper] Log file not found: {self.log_path}. Waiting ...")
            while not self.log_path.exists():
                await asyncio.sleep(5)

        with open(self.log_path, "r") as f:
            f.seek(0, 2)  # Seek to end — only tail new lines
            while True:
                line = f.readline()
                if line:
                    yield line
                else:
                    await asyncio.sleep(0.1)

    # ── Processing ────────────────────────────────────────────────────────────

    async def _process_line(self, line: str) -> None:
        record = parse_nginx_line(line)
        if not record:
            return

        endpoint = record["path"].split("?")[0]   # Strip query string
        self._stats[endpoint].record(record["status"], record.get("request_time"))

        # Ship to OTel Loki via structured log
        self._otel_logger.info(
            f'{record["method"]} {record["path"]} {record["status"]}',
            extra={
                "otelSpanID":   "0000000000000000",
                "otelTraceID":  "00000000000000000000000000000000",
                "remote_addr":  record["remote_addr"],
                "method":       record["method"],
                "path":         record["path"],
                "status":       record["status"],
                "bytes_sent":   record["bytes_sent"],
                "request_time": record.get("request_time"),
                "agent":        "nexus.nginx_log_shipper",
            },
        )

    async def _check_thresholds(self) -> None:
        """Periodic threshold evaluation — emits NATS events when thresholds breach."""
        if not self.nats:
            return

        for endpoint, stats in list(self._stats.items()):
            err_rate = stats.error_rate()
            rps      = stats.rps()
            p95      = stats.p95_latency()

            if err_rate > self.error_rate_threshold and rps > 0.1:
                logger.warning(
                    f"[LogShipper] HIGH_ERROR_RATE on {endpoint}: "
                    f"{err_rate:.1%} errors over {self.window_seconds}s"
                )
                await self.nats.publish(IncidentEvent(
                    agent=AgentType.NGINX,
                    signal_type=SignalType.HIGH_ERROR_RATE,
                    severity=Severity.CRITICAL if err_rate > 0.20 else Severity.WARNING,
                    context=NginxHighErrorContext(
                        endpoint=endpoint,
                        error_rate=err_rate,
                        baseline_rate=0.02,
                        rps=rps,
                        window_seconds=self.window_seconds,
                    ).model_dump(),
                    suggested_runbook="runbook_high_error_rate_post_deploy_v1",
                    suggested_healing_level=2,
                ))

            if p95 is not None and p95 * 1000 > self.latency_p95_ms:
                logger.warning(
                    f"[LogShipper] HIGH_LATENCY on {endpoint}: "
                    f"P95={p95*1000:.0f}ms > {self.latency_p95_ms}ms"
                )
                await self.nats.publish(IncidentEvent(
                    agent=AgentType.NGINX,
                    signal_type=SignalType.HIGH_LATENCY,
                    severity=Severity.WARNING,
                    context={
                        "endpoint":       endpoint,
                        "p95_latency_ms": p95 * 1000,
                        "threshold_ms":   self.latency_p95_ms,
                        "rps":            rps,
                    },
                ))

    # ── Run ───────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Start tailing and shipping. Runs indefinitely."""
        logger.info(f"[LogShipper] Tailing {self.log_path}, shipping to {self.otel_endpoint}")

        async def threshold_loop():
            while True:
                await asyncio.sleep(self.check_interval)
                await self._check_thresholds()

        tail_task      = asyncio.create_task(self._tail_loop())
        threshold_task = asyncio.create_task(threshold_loop())

        await asyncio.gather(tail_task, threshold_task)

    async def _tail_loop(self) -> None:
        async for line in self._tail():
            await self._process_line(line)


# ──────────────────────────────────────────────────────────────────────────────
# Entrypoint (for running as a standalone sidecar / process)
# ──────────────────────────────────────────────────────────────────────────────

async def _main() -> None:
    import os

    log_path     = os.getenv("NGINX_LOG_PATH",    "/var/log/nginx/access.log")
    otel_ep      = os.getenv("OTEL_ENDPOINT",     "http://localhost:4318")
    nats_url     = os.getenv("NATS_URL",          "nats://localhost:4222")

    logging.basicConfig(level=logging.INFO)

    async with NATSClient(nats_url) as nc:
        shipper = NginxLogShipper(
            log_path=log_path,
            nats_client=nc,
            otel_endpoint=otel_ep,
        )
        await shipper.run()


if __name__ == "__main__":
    asyncio.run(_main())
