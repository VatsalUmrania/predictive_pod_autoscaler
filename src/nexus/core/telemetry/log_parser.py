"""
NGINX Log Parsing and Statistics
================================
Utilities for parsing NGINX combined log entries and maintaining rolling window
endpoint performance and error statistics.
"""

from __future__ import annotations

import re
import time
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.core.bus.events.event import SymptomSignal

# NGINX Combined Log Format parser
_COMBINED_RE = re.compile(
    r"(?P<remote_addr>\S+)\s+-\s+\S+\s+"
    r"\[(?P<time_local>[^\]]+)\]\s+"
    r'"(?P<method>\S+)\s+(?P<path>\S+)\s+\S+"\s+'
    r"(?P<status>\d+)\s+"
    r"(?P<bytes_sent>\d+)\s+"
    r'"(?P<referer>[^"]*)"\s+'
    r'"(?P<user_agent>[^"]*)"'
    r"(?:\s+(?P<request_time>\d+\.\d+))?"  # Optional: $request_time
)

_NGINX_TIME_FMT = "%d/%b/%Y:%H:%M:%S %z"


def parse_nginx_line(line: str) -> dict | None:
    """Parse a single NGINX combined log line. Returns None on parse failure."""
    m = _COMBINED_RE.match(line.strip())
    if not m:
        return None
    d = m.groupdict()
    try:
        d["status"] = int(d["status"])
        d["bytes_sent"] = int(d["bytes_sent"])
        d["request_time"] = float(d["request_time"]) if d.get("request_time") else None
        d["timestamp"] = datetime.strptime(d["time_local"], _NGINX_TIME_FMT)
    except (ValueError, TypeError):
        return None
    return d


class EndpointStats:
    """Rolling-window per-endpoint statistics for anomaly detection."""

    def __init__(self, window_seconds: int = 60):
        self.window = window_seconds
        # deque of (timestamp, status_code, request_time_or_None)
        self._records: deque = deque()

    def record(self, status: int, request_time: float | None) -> None:
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

    def p95_latency(self) -> float | None:
        times = [t for _, _, t in self._records if t is not None]
        if not times:
            return None
        times.sort()
        idx = int(len(times) * 0.95)
        return float(times[min(idx, len(times) - 1)])


# ── Universal Symptom Ingestion Log Parser ───────────────────────────────────

_ERROR_LEVELS = frozenset({"ERROR", "CRITICAL", "FATAL"})
_TRACEBACK_MARKERS = ("Traceback (most recent call last)", "Exception:", "Error:")


def is_error_log(line_or_record: str | dict) -> bool:
    """Check if a log entry represents an error, fatal exception, or HTTP >= 500."""
    if isinstance(line_or_record, dict):
        lvl = str(line_or_record.get("level", "")).upper()
        if lvl in _ERROR_LEVELS:
            return True
        status = line_or_record.get("status")
        if status is not None:
            try:
                if int(status) >= 500:
                    return True
            except (ValueError, TypeError):
                pass
        msg = str(line_or_record.get("message", ""))
        return any(marker in msg for marker in _TRACEBACK_MARKERS)

    text = str(line_or_record)
    if any(lvl in text.upper() for lvl in _ERROR_LEVELS):
        return True
    if any(marker in text for marker in _TRACEBACK_MARKERS):
        return True
    nginx_dict = parse_nginx_line(text)
    if nginx_dict and nginx_dict.get("status", 0) >= 500:
        return True
    return False


def parse_and_group_log_burst(
    logs: list[str | dict],
    resource_urn: str,
    provider: str = "kubernetes",
    region_or_namespace: str = "default",
    burst_threshold: int = 1,
) -> SymptomSignal | None:
    """Detect error logs and emit a SymptomSignal(kind=SignalKind.ERROR_LOG_BURST)."""
    from nexus.core.bus.events.enums import Severity, SignalKind, SignalSource
    from nexus.core.bus.events.event import SymptomSignal

    error_lines = [log for log in logs if is_error_log(log)]
    if len(error_lines) < burst_threshold:
        return None

    src = (
        SignalSource.AWS_CLOUDWATCH
        if provider.lower() == "aws"
        else SignalSource.KUBERNETES_API
    )
    summary = (
        f"Detected {len(error_lines)} error/fatal log(s) for resource {resource_urn}"
    )
    raw = {
        "error_logs": [str(err) for err in error_lines[:50]],
        "total_error_count": len(error_lines),
    }

    return SymptomSignal(
        source=src,
        kind=SignalKind.ERROR_LOG_BURST,
        provider=provider,
        resource_urn=resource_urn,
        region_or_namespace=region_or_namespace,
        symptom_summary=summary,
        raw_payload=raw,
        severity=Severity.HIGH,
    )
