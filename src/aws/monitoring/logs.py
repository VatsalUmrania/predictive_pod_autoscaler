"""
Phase 2 — Monitoring: CloudWatch Logs
=======================================
Wraps boto3 CloudWatch Logs into clean Python objects.
No AI. No decisions. Just log retrieval.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


@dataclass
class LogEvent:
    """A single log line from CloudWatch Logs."""
    timestamp: datetime
    message: str
    log_stream: str


@dataclass
class LogSummary:
    """Collected logs for a resource over a time window."""
    log_group: str
    window_minutes: int
    events: list[LogEvent] = field(default_factory=list)
    error_lines: list[str] = field(default_factory=list)
    total_count: int = 0

    @property
    def has_errors(self) -> bool:
        return len(self.error_lines) > 0


def get_lambda_logs(
    logs_client,
    function_name: str,
    window_minutes: int = 5,
    max_events: int = 100,
    filter_pattern: str = "?ERROR ?Exception ?Error ?CRITICAL ?Task timed out ?OOM ?Runtime exited",
) -> LogSummary:
    """
    Fetch recent error log lines from a Lambda log group.
    Returns LogSummary with error_lines pre-filtered.
    """
    log_group = f"/aws/lambda/{function_name}"
    return _fetch_logs(logs_client, log_group, window_minutes, max_events, filter_pattern)


def get_apigw_logs(
    logs_client,
    api_id: str,
    stage: str = "prod",
    window_minutes: int = 5,
    max_events: int = 50,
) -> LogSummary:
    """Fetch API Gateway access logs."""
    log_group = f"API-Gateway-Execution-Logs_{api_id}/{stage}"
    return _fetch_logs(logs_client, log_group, window_minutes, max_events, "?5XX ?ERROR ?error")


def get_all_log_groups(logs_client, prefix: str = "") -> list[str]:
    """List all CloudWatch log group names, optionally filtered by prefix."""
    groups = []
    try:
        kwargs = {"logGroupNamePrefix": prefix} if prefix else {}
        paginator = logs_client.get_paginator("describe_log_groups")
        for page in paginator.paginate(**kwargs):
            groups.extend(g["logGroupName"] for g in page.get("logGroups", []))
    except Exception as exc:
        logger.debug(f"[Logs] get_all_log_groups: {exc}")
    return groups


# ── Internal ──────────────────────────────────────────────────────────────────

def _fetch_logs(
    logs_client,
    log_group: str,
    window_minutes: int,
    max_events: int,
    filter_pattern: str,
) -> LogSummary:
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - (window_minutes * 60 * 1000)
    events: list[LogEvent] = []
    error_lines: list[str] = []

    try:
        resp = logs_client.filter_log_events(
            logGroupName=log_group,
            startTime=start_ms,
            endTime=end_ms,
            filterPattern=filter_pattern,
            limit=max_events,
        )
        for raw in resp.get("events", []):
            msg = raw.get("message", "").strip()
            ts = datetime.fromtimestamp(raw.get("timestamp", 0) / 1000, tz=timezone.utc)
            ev = LogEvent(timestamp=ts, message=msg[:500], log_stream=raw.get("logStreamName", ""))
            events.append(ev)
            error_lines.append(msg[:300])

    except logs_client.exceptions.ResourceNotFoundException:
        logger.debug(f"[Logs] Log group not found: {log_group}")
    except Exception as exc:
        logger.debug(f"[Logs] fetch_logs({log_group}): {exc}")

    return LogSummary(
        log_group=log_group,
        window_minutes=window_minutes,
        events=events,
        error_lines=error_lines,
        total_count=len(events),
    )
