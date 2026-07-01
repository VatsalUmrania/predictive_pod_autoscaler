"""
Phase 2 — Monitoring: CloudWatch Metrics
==========================================
Wraps boto3 GetMetricData into clean Python objects.
No AI. No decisions. Just data retrieval.

Usage:
    client = boto3.client("cloudwatch", region_name="us-east-1")
    rate = get_lambda_error_rate(client, "my-function", window_minutes=5)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MetricPoint:
    """A single CloudWatch metric datapoint."""
    timestamp: datetime
    value: float
    unit: str


@dataclass
class LambdaMetrics:
    """Collected Lambda metrics for one function over a time window."""
    function_name: str
    window_minutes: int
    error_rate: float        # 0.0–1.0
    invocation_count: float
    error_count: float
    throttle_count: float
    duration_p50_ms: float
    duration_p99_ms: float
    duration_max_ms: float
    timeout_ms: float        # configured timeout × 1000
    memory_mb: int


@dataclass
class SQSMetrics:
    """Collected SQS queue metrics."""
    queue_name: str
    visible_messages: int
    not_visible_messages: int
    oldest_message_age_seconds: float


@dataclass
class DynamoMetrics:
    """Collected DynamoDB table metrics."""
    table_name: str
    read_throttles: int
    write_throttles: int
    system_errors: int


@dataclass
class APIGatewayMetrics:
    """Collected API Gateway metrics."""
    api_name: str
    count_5xx: float
    count_4xx: float
    latency_p99_ms: float
    integration_latency_p99_ms: float


# ── Lambda ────────────────────────────────────────────────────────────────────

def get_lambda_error_rate(cw, function_name: str, window_minutes: int = 5) -> float:
    """Return error rate 0.0–1.0 for a Lambda function. Returns 0.0 on any error."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=window_minutes)
    period = window_minutes * 60
    dims = [{"Name": "FunctionName", "Value": function_name}]
    try:
        errors = _sum(cw, "AWS/Lambda", "Errors", dims, start, end, period)
        invocations = _sum(cw, "AWS/Lambda", "Invocations", dims, start, end, period)
        return (errors / invocations) if invocations > 0 else 0.0
    except Exception as exc:
        logger.debug(f"[Metrics] error_rate({function_name}): {exc}")
        return 0.0


def get_lambda_throttles(cw, function_name: str, window_minutes: int = 5) -> int:
    """Return total throttle event count."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=window_minutes)
    dims = [{"Name": "FunctionName", "Value": function_name}]
    try:
        return int(_sum(cw, "AWS/Lambda", "Throttles", dims, start, end, window_minutes * 60))
    except Exception as exc:
        logger.debug(f"[Metrics] throttles({function_name}): {exc}")
        return 0


def get_lambda_duration(cw, function_name: str, window_minutes: int = 5) -> dict[str, float]:
    """Return p50, p99, max duration in milliseconds."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=window_minutes)
    dims = [{"Name": "FunctionName", "Value": function_name}]
    try:
        resp = cw.get_metric_statistics(
            Namespace="AWS/Lambda", MetricName="Duration",
            Dimensions=dims, StartTime=start, EndTime=end,
            Period=window_minutes * 60,
            ExtendedStatistics=["p50", "p99"], Statistics=["Maximum"],
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
        logger.debug(f"[Metrics] duration({function_name}): {exc}")
        return {"p50": 0.0, "p99": 0.0, "max": 0.0}


def collect_lambda_metrics(cw, function_name: str, timeout_ms: float = 3000,
                           memory_mb: int = 128, window_minutes: int = 5) -> LambdaMetrics:
    """Collect all Lambda metrics in one call. Returns a typed LambdaMetrics object."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=window_minutes)
    dims = [{"Name": "FunctionName", "Value": function_name}]
    period = window_minutes * 60

    errors = _sum(cw, "AWS/Lambda", "Errors", dims, start, end, period)
    invocations = _sum(cw, "AWS/Lambda", "Invocations", dims, start, end, period)
    throttles = _sum(cw, "AWS/Lambda", "Throttles", dims, start, end, period)
    duration = get_lambda_duration(cw, function_name, window_minutes)

    return LambdaMetrics(
        function_name=function_name,
        window_minutes=window_minutes,
        error_rate=(errors / invocations) if invocations > 0 else 0.0,
        invocation_count=invocations,
        error_count=errors,
        throttle_count=throttles,
        duration_p50_ms=duration["p50"],
        duration_p99_ms=duration["p99"],
        duration_max_ms=duration["max"],
        timeout_ms=timeout_ms,
        memory_mb=memory_mb,
    )


# ── SQS ───────────────────────────────────────────────────────────────────────

def collect_sqs_metrics(cw, queue_name: str, window_minutes: int = 5) -> SQSMetrics:
    """Collect SQS queue depth and age metrics."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=window_minutes)
    dims = [{"Name": "QueueName", "Value": queue_name}]
    period = window_minutes * 60
    try:
        visible = _max(cw, "AWS/SQS", "ApproximateNumberOfMessagesVisible", dims, start, end, period)
        not_visible = _max(cw, "AWS/SQS", "ApproximateNumberOfMessagesNotVisible", dims, start, end, period)
        age = _max(cw, "AWS/SQS", "ApproximateAgeOfOldestMessage", dims, start, end, period)
        return SQSMetrics(
            queue_name=queue_name,
            visible_messages=int(visible),
            not_visible_messages=int(not_visible),
            oldest_message_age_seconds=age,
        )
    except Exception as exc:
        logger.debug(f"[Metrics] sqs({queue_name}): {exc}")
        return SQSMetrics(queue_name=queue_name, visible_messages=0,
                          not_visible_messages=0, oldest_message_age_seconds=0)


# ── DynamoDB ──────────────────────────────────────────────────────────────────

def collect_dynamo_metrics(cw, table_name: str, window_minutes: int = 5) -> DynamoMetrics:
    """Collect DynamoDB throttle and error metrics."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=window_minutes)
    dims = [{"Name": "TableName", "Value": table_name}]
    period = window_minutes * 60
    try:
        read_t = int(_sum(cw, "AWS/DynamoDB", "ReadThrottleEvents", dims, start, end, period))
        write_t = int(_sum(cw, "AWS/DynamoDB", "WriteThrottleEvents", dims, start, end, period))
        sys_err = int(_sum(cw, "AWS/DynamoDB", "SystemErrors", dims, start, end, period))
        return DynamoMetrics(table_name=table_name, read_throttles=read_t,
                             write_throttles=write_t, system_errors=sys_err)
    except Exception as exc:
        logger.debug(f"[Metrics] dynamo({table_name}): {exc}")
        return DynamoMetrics(table_name=table_name, read_throttles=0,
                             write_throttles=0, system_errors=0)


# ── API Gateway ───────────────────────────────────────────────────────────────

def collect_apigw_metrics(cw, api_name: str, stage: str = "prod",
                          window_minutes: int = 5) -> APIGatewayMetrics:
    """Collect API Gateway 5xx, 4xx, and latency metrics."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=window_minutes)
    dims = [{"Name": "ApiName", "Value": api_name}, {"Name": "Stage", "Value": stage}]
    period = window_minutes * 60
    try:
        c5xx = _sum(cw, "AWS/ApiGateway", "5XXError", dims, start, end, period)
        c4xx = _sum(cw, "AWS/ApiGateway", "4XXError", dims, start, end, period)
        lat = _p99(cw, "AWS/ApiGateway", "Latency", dims, start, end, period)
        int_lat = _p99(cw, "AWS/ApiGateway", "IntegrationLatency", dims, start, end, period)
        return APIGatewayMetrics(api_name=api_name, count_5xx=c5xx, count_4xx=c4xx,
                                 latency_p99_ms=lat, integration_latency_p99_ms=int_lat)
    except Exception as exc:
        logger.debug(f"[Metrics] apigw({api_name}): {exc}")
        return APIGatewayMetrics(api_name=api_name, count_5xx=0, count_4xx=0,
                                 latency_p99_ms=0, integration_latency_p99_ms=0)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _sum(cw, ns, metric, dims, start, end, period) -> float:
    resp = cw.get_metric_statistics(
        Namespace=ns, MetricName=metric, Dimensions=dims,
        StartTime=start, EndTime=end, Period=period, Statistics=["Sum"],
    )
    return sum(dp.get("Sum", 0.0) for dp in resp.get("Datapoints", []))


def _max(cw, ns, metric, dims, start, end, period) -> float:
    resp = cw.get_metric_statistics(
        Namespace=ns, MetricName=metric, Dimensions=dims,
        StartTime=start, EndTime=end, Period=period, Statistics=["Maximum"],
    )
    dps = resp.get("Datapoints", [])
    return max((dp.get("Maximum", 0.0) for dp in dps), default=0.0)


def _p99(cw, ns, metric, dims, start, end, period) -> float:
    resp = cw.get_metric_statistics(
        Namespace=ns, MetricName=metric, Dimensions=dims,
        StartTime=start, EndTime=end, Period=period, ExtendedStatistics=["p99"],
    )
    dps = resp.get("Datapoints", [])
    return max((dp.get("ExtendedStatistics", {}).get("p99", 0.0) for dp in dps), default=0.0)
