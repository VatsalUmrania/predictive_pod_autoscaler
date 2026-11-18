"""NEXUS NGINX Log Shipper: tails access.log and ships logs to OTel Collector."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import AsyncIterator
from pathlib import Path

from nexus.bus.events import (
    AgentType,
    IncidentEvent,
    NginxHighErrorContext,
    Severity,
    SignalType,
)
from nexus.bus.nats.client import NATSClient
from nexus.telemetry.log_parser import (
    _COMBINED_RE,
    _NGINX_TIME_FMT,
    EndpointStats,
    parse_nginx_line,
)
from nexus.telemetry.otel import setup_nginx_otel_logger

logger = logging.getLogger(__name__)

__all__ = [
    "_COMBINED_RE",
    "_NGINX_TIME_FMT",
    "EndpointStats",
    "parse_nginx_line",
    "NginxLogShipper",
]


class NginxLogShipper:
    """Async NGINX access log tailer with OTel shipping and NATS anomaly events."""

    def __init__(
        self,
        log_path: str = "/var/log/nginx/access.log",
        nats_client: NATSClient | None = None,
        otel_endpoint: str = "http://localhost:4318",
        error_rate_threshold: float = 0.05,
        latency_p95_ms: float = 500.0,
        window_seconds: int = 60,
        check_interval: int = 15,
    ):
        self.log_path = Path(log_path)
        self.nats = nats_client
        self.otel_endpoint = otel_endpoint
        self.error_rate_threshold = error_rate_threshold
        self.latency_p95_ms = latency_p95_ms
        self.window_seconds = window_seconds
        self.check_interval = check_interval

        self._stats: dict[str, EndpointStats] = defaultdict(
            lambda: EndpointStats(window_seconds)
        )
        self._otel_logger = setup_nginx_otel_logger(self.otel_endpoint)

    async def _tail(self) -> AsyncIterator[str]:
        """Async generator that yields new lines as NGINX writes them."""
        if not self.log_path.exists():
            logger.warning(
                f"[LogShipper] Log file not found: {self.log_path}. Waiting ..."
            )
            while not self.log_path.exists():
                await asyncio.sleep(5)

        with open(self.log_path) as f:
            f.seek(0, 2)
            while True:
                line = f.readline()
                if line:
                    yield line
                else:
                    await asyncio.sleep(0.1)

    async def _process_line(self, line: str) -> None:
        record = parse_nginx_line(line)
        if not record:
            return

        endpoint = record["path"].split("?")[0]
        self._stats[endpoint].record(record["status"], record.get("request_time"))

        self._otel_logger.info(
            f'{record["method"]} {record["path"]} {record["status"]}',
            extra={
                "otelSpanID": "0000000000000000",
                "otelTraceID": "00000000000000000000000000000000",
                "remote_addr": record["remote_addr"],
                "method": record["method"],
                "path": record["path"],
                "status": record["status"],
                "bytes_sent": record["bytes_sent"],
                "request_time": record.get("request_time"),
                "agent": "nexus.nginx_log_shipper",
            },
        )

    async def _check_thresholds(self) -> None:
        """Periodic threshold evaluation — emits NATS events when thresholds breach."""
        if not self.nats:
            return

        for endpoint, stats in list(self._stats.items()):
            err_rate, rps, p95 = stats.error_rate(), stats.rps(), stats.p95_latency()

            if err_rate > self.error_rate_threshold and rps > 0.1:
                logger.warning(
                    f"[LogShipper] HIGH_ERROR_RATE on {endpoint}: "
                    f"{err_rate:.1%} errors over {self.window_seconds}s"
                )
                await self.nats.publish(
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
                            window_seconds=self.window_seconds,
                        ).model_dump(),
                        suggested_healing_level=2,
                    )
                )

            if p95 is not None and p95 * 1000 > self.latency_p95_ms:
                logger.warning(
                    f"[LogShipper] HIGH_LATENCY on {endpoint}: "
                    f"P95={p95*1000:.0f}ms > {self.latency_p95_ms}ms"
                )
                await self.nats.publish(
                    IncidentEvent(
                        agent=AgentType.NGINX,
                        signal_type=SignalType.HIGH_LATENCY,
                        severity=Severity.WARNING,
                        context={
                            "endpoint": endpoint,
                            "p95_latency_ms": p95 * 1000,
                            "threshold_ms": self.latency_p95_ms,
                            "rps": rps,
                        },
                    )
                )

    async def run(self) -> None:
        """Start tailing and shipping. Runs indefinitely."""
        logger.info(
            f"[LogShipper] Tailing {self.log_path}, shipping to {self.otel_endpoint}"
        )

        async def threshold_loop():
            while True:
                await asyncio.sleep(self.check_interval)
                await self._check_thresholds()

        tail_task = asyncio.create_task(self._tail_loop())
        threshold_task = asyncio.create_task(threshold_loop())
        await asyncio.gather(tail_task, threshold_task)

    async def _tail_loop(self) -> None:
        async for line in self._tail():
            await self._process_line(line)


async def _main() -> None:
    import os

    log_path = os.getenv("NGINX_LOG_PATH", "/var/log/nginx/access.log")
    otel_ep = os.getenv("OTEL_ENDPOINT", "http://localhost:4318")
    nats_url = os.getenv("NATS_URL", "nats://localhost:4222")

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
