"""
CloudWatch Tools
=================
Pure functions for querying CloudWatch metrics and logs.
No classes. No side effects. Just boto3 calls that return data.

Every function:
  - Accepts an explicit boto3 client (easy to mock in tests)
  - Returns a typed dict or list
  - Never raises — returns empty/zero on error
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ── Metric queries ────────────────────────────────────────────────────────────

def get_lambda_error_rate(
    cw_client,
    function_name: str,
    window_minutes: int = 5,
) -> float:
    """
    Return error rate (0.0–1.0) for a Lambda function over the past window_minutes.
    Returns 0.0 if no invocations or on any error.
    """
    dims = [{"Name": "FunctionName", "Value": function_name}]
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=window_minutes)
    period = window_minutes * 60

    try:
        errors = _get_metric_sum(cw_client, "AWS/Lambda", "Errors", dims, start, end, period)
        invocations = _get_metric_sum(cw_client, "AWS/Lambda", "Invocations", dims, start, end, period)
        return (errors / invocations) if invocations > 0 else 0.0
    except Exception as exc:
        logger.debug(f"[CloudWatch] get_lambda_error_rate({function_name}): {exc}")
        return 0.0


def get_lambda_throttles(
    cw_client,
    function_name: str,
    window_minutes: int = 5,
) -> int:
    """Return total throttle count for a Lambda function."""
    dims = [{"Name": "FunctionName", "Value": function_name}]
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=window_minutes)
    period = window_minutes * 60
    try:
        return int(_get_metric_sum(cw_client, "AWS/Lambda", "Throttles", dims, start, end, period))
    except Exception as exc:
        logger.debug(f"[CloudWatch] get_lambda_throttles({function_name}): {exc}")
        return 0


def get_lambda_duration_stats(
    cw_client,
    function_name: str,
    window_minutes: int = 5,
) -> dict[str, float]:
    """Return p50, p99, max duration in milliseconds."""
    dims = [{"Name": "FunctionName", "Value": function_name}]
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=window_minutes)
    period = window_minutes * 60
    try:
        resp = cw_client.get_metric_statistics(
            Namespace="AWS/Lambda",
            MetricName="Duration",
            Dimensions=dims,
            StartTime=start,
            EndTime=end,
            Period=period,
            ExtendedStatistics=["p50", "p99"],
            Statistics=["Maximum"],
        )
        dps = resp.get("Datapoints", [])
        if not dps:
            return {"p50": 0.0, "p99": 0.0, "max": 0.0}
        dp = dps[0]
        return {
            "p50": dp.get("ExtendedStatistics", {}).get("p50", 0.0),
            "p99": dp.get("ExtendedStatistics", {}).get("p99", 0.0),
            "max": dp.get("Maximum", 0.0),
        }
    except Exception as exc:
        logger.debug(f"[CloudWatch] get_lambda_duration_stats({function_name}): {exc}")
        return {"p50": 0.0, "p99": 0.0, "max": 0.0}


def get_sqs_dlq_depth(
    cw_client,
    queue_name: str,
    window_minutes: int = 5,
) -> int:
    """Return number of visible messages in a DLQ."""
    dims = [{"Name": "QueueName", "Value": queue_name}]
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=window_minutes)
    period = window_minutes * 60
    try:
        return int(_get_metric_max(
            cw_client, "AWS/SQS",
            "ApproximateNumberOfMessagesVisible",
            dims, start, end, period,
        ))
    except Exception as exc:
        logger.debug(f"[CloudWatch] get_sqs_dlq_depth({queue_name}): {exc}")
        return 0


def get_dynamo_throttles(
    cw_client,
    table_name: str,
    window_minutes: int = 5,
) -> dict[str, int]:
    """Return read and write throttle counts for a DynamoDB table."""
    dims = [{"Name": "TableName", "Value": table_name}]
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=window_minutes)
    period = window_minutes * 60
    try:
        read = int(_get_metric_sum(
            cw_client, "AWS/DynamoDB", "ReadThrottleEvents", dims, start, end, period
        ))
        write = int(_get_metric_sum(
            cw_client, "AWS/DynamoDB", "WriteThrottleEvents", dims, start, end, period
        ))
        return {"read": read, "write": write}
    except Exception as exc:
        logger.debug(f"[CloudWatch] get_dynamo_throttles({table_name}): {exc}")
        return {"read": 0, "write": 0}


# ── Log queries ───────────────────────────────────────────────────────────────

def get_lambda_error_logs(
    logs_client,
    function_name: str,
    max_lines: int = 50,
    window_minutes: int = 5,
) -> list[str]:
    """
    Fetch recent ERROR log lines from a Lambda function's log group.
    Returns [] if log group doesn't exist or on any error.
    """
    log_group = f"/aws/lambda/{function_name}"
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - (window_minutes * 60 * 1000)
    try:
        resp = logs_client.filter_log_events(
            logGroupName=log_group,
            startTime=start_ms,
            endTime=end_ms,
            filterPattern="?ERROR ?Exception ?Error ?CRITICAL ?OOM ?timeout",
            limit=max_lines,
        )
        return [e.get("message", "").strip() for e in resp.get("events", [])]
    except Exception as exc:
        logger.debug(f"[CloudWatch] get_lambda_error_logs({function_name}): {exc}")
        return []


# ── Internal helpers ──────────────────────────────────────────────────────────

def _get_metric_sum(
    cw_client,
    namespace: str,
    metric_name: str,
    dimensions: list[dict],
    start: datetime,
    end: datetime,
    period: int,
) -> float:
    resp = cw_client.get_metric_statistics(
        Namespace=namespace,
        MetricName=metric_name,
        Dimensions=dimensions,
        StartTime=start,
        EndTime=end,
        Period=period,
        Statistics=["Sum"],
    )
    return sum(dp.get("Sum", 0.0) for dp in resp.get("Datapoints", []))


def _get_metric_max(
    cw_client,
    namespace: str,
    metric_name: str,
    dimensions: list[dict],
    start: datetime,
    end: datetime,
    period: int,
) -> float:
    resp = cw_client.get_metric_statistics(
        Namespace=namespace,
        MetricName=metric_name,
        Dimensions=dimensions,
        StartTime=start,
        EndTime=end,
        Period=period,
        Statistics=["Maximum"],
    )
    dps = resp.get("Datapoints", [])
    return max((dp.get("Maximum", 0.0) for dp in dps), default=0.0)
