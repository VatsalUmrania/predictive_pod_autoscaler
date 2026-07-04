"""
Lambda Handler
===============
This is the entry point for AWS Lambda. EventBridge calls this function
whenever a CloudWatch Alarm fires.

EventBridge event format (CloudWatch Alarm State Change):
    {
      "version": "0",
      "id": "...",
      "detail-type": "CloudWatch Alarm State Change",
      "source": "aws.cloudwatch",
      "account": "123456789012",
      "time": "2024-01-01T00:00:00Z",
      "region": "us-east-1",
      "detail": {
        "alarmName": "my-lambda-errors",
        "state": {
          "value": "ALARM",
          "reason": "Threshold Crossed: 1 out of the last 1 datapoints..."
        },
        "previousState": {"value": "OK"},
        "configuration": {
          "metrics": [...]
        }
      }
    }

Also supports direct invocation for testing:
    {
      "resource_name": "my-lambda",
      "signal_type":   "lambda_error_rate_high",
      "raw_metrics":   {},
      "region":        "us-east-1"
    }

Deployment:
    - Set Lambda timeout to at least 5 minutes (300s) to allow verify wait
    - Attach IAM role with CloudWatch Read + Lambda Write permissions
    - Set ANTHROPIC_API_KEY and SLACK_WEBHOOK_URL in Lambda environment variables
"""

from __future__ import annotations

import json
import logging
import os

from aws.graph.workflow import run_graph

# Configure logging at cold start
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Map alarm names to signal types (customize for your infrastructure)
# Format: "alarm-name-pattern" → "signal_type"
# Uses prefix matching — first match wins
ALARM_SIGNAL_MAP = {
    "lambda-errors":    "lambda_error_rate_high",
    "lambda-throttles": "lambda_throttle_spike",
    "lambda-timeout":   "lambda_timeout",
    "lambda-oom":       "lambda_oom",
    "sqs-dlq":          "sqs_dlq_depth_high",
    "dynamo-throttle":  "dynamo_throttle_read",
    "apigw-5xx":        "apigw_5xx_spike",
}


def lambda_handler(event: dict, context) -> dict:
    """
    AWS Lambda entry point.

    Parses the EventBridge alarm event, maps it to an incident dict,
    and runs the full LangGraph pipeline.
    """
    while isinstance(event, str):
        try:
            parsed = json.loads(event)
            if parsed == event:
                break
            event = parsed
        except json.JSONDecodeError as exc:
            logger.error("[Handler] Failed to parse event JSON string: %s", exc)
            return {"statusCode": 400, "body": f"Invalid JSON event string: {exc}"}

    if not isinstance(event, dict):
        logger.error("[Handler] Event must be a dict, got %s", type(event))
        return {"statusCode": 400, "body": "Event must be a JSON object"}

    # Handle API Gateway Proxy events
    if "body" in event and isinstance(event["body"], str):
        try:
            event = json.loads(event["body"])
        except json.JSONDecodeError as exc:
            logger.error("[Handler] Failed to parse API Gateway body JSON: %s", exc)
            return {"statusCode": 400, "body": f"Invalid JSON in request body: {exc}"}

    # Re-check in case the body was parsed but is not a dict
    if not isinstance(event, dict):
        return {"statusCode": 400, "body": "Parsed API Gateway body is not a JSON object"}

    logger.info("[Handler] Received event: %s", json.dumps(event)[:500])

    # ── Parse the incoming event ──────────────────────────────────────────────
    try:
        incident = _parse_event(event)
    except Exception as exc:
        logger.error("[Handler] Failed to parse event: %s", exc)
        return {"statusCode": 400, "body": f"Invalid event: {exc}"}

    # ── Ignore non-ALARM states ───────────────────────────────────────────────
    if incident is None:
        logger.info("[Handler] Event is not an ALARM state — skipping")
        return {"statusCode": 200, "body": "skipped"}

    logger.info("[Handler] Processing incident: %s / %s", incident['resource_name'], incident['signal_type'])

    # ── Run the graph ─────────────────────────────────────────────────────────
    result = run_graph(incident)

    # ── Return summary ────────────────────────────────────────────────────────
    summary = {
        "resource":   result.get("resource_name"),
        "signal":     result.get("signal_type"),
        "action":     result.get("rca", {}).get("action"),
        "confidence": result.get("rca", {}).get("confidence"),
        "resolved":   result.get("resolved"),
        "escalated":  result.get("escalated"),
    }
    logger.info("[Handler] Done: %s", summary)
    return {"statusCode": 200, "body": json.dumps(summary)}


def _parse_event(event: dict) -> dict | None:
    """
    Convert an EventBridge CloudWatch Alarm event into an incident dict.

    Returns None if the event should be ignored (e.g., alarm returning to OK).
    Returns a direct incident dict if event is a test invocation.
    """

    # ── Direct test invocation (has resource_name field) ─────────────────────
    if "resource_name" in event:
        return {
            "resource_name": event["resource_name"],
            "signal_type":   event.get("signal_type", "lambda_error_rate_high"),
            "raw_metrics":   event.get("raw_metrics", {}),
            "region":        event.get("region", os.getenv("AWS_DEFAULT_REGION", "us-east-1")),
        }

    # ── EventBridge CloudWatch Alarm event ───────────────────────────────────
    detail_type = event.get("detail-type", "")
    if detail_type != "CloudWatch Alarm State Change":
        logger.info("[Handler] Ignoring non-alarm event: %s", detail_type)
        return None

    detail = event.get("detail", {})
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.debug("[Handler] Could not parse detail as JSON, using empty dict")
            detail = {}
    if not isinstance(detail, dict):
        detail = {}

    state = detail.get("state", {})
    state_value = state.get("value", "") if isinstance(state, dict) else str(state)
    alarm_name = detail.get("alarmName", "unknown")
    region = event.get("region", os.getenv("AWS_DEFAULT_REGION", "us-east-1"))

    # Only respond to ALARM state (not OK or INSUFFICIENT_DATA)
    if state_value != "ALARM":
        logger.info("[Handler] Alarm '%s' is in state '%s' — ignoring", alarm_name, state_value)
        return None

    # Map alarm name to signal type
    signal_type = _map_alarm_to_signal(alarm_name)

    # Extract resource name — prefer dimensions (authoritative), fall back to alarm name parsing
    resource_name = _extract_resource_from_detail(detail, alarm_name)

    # Extract any dimension values as raw context
    raw_metrics = _extract_metrics_from_alarm(detail)

    return {
        "resource_name": resource_name,
        "signal_type":   signal_type,
        "raw_metrics":   raw_metrics,
        "region":        region,
    }


def _map_alarm_to_signal(alarm_name: str) -> str:
    """
    Map a CloudWatch alarm name to a signal type string.

    Uses prefix matching against ALARM_SIGNAL_MAP.
    Falls back to "aws_alarm_fired" for unknown alarms.
    """
    alarm_lower = alarm_name.lower()
    for pattern, signal in ALARM_SIGNAL_MAP.items():
        if pattern in alarm_lower:
            return signal
    logger.info("[Handler] No signal mapping for alarm '%s' — using aws_alarm_fired", alarm_name)
    return "aws_alarm_fired"


def _extract_resource_from_detail(detail: dict, alarm_name: str) -> str:
    """
    Extract the resource name from alarm metric dimensions (the reliable source).

    The EventBridge CloudWatch Alarm event carries explicit dimension values like
    FunctionName, QueueName, or TableName inside
    detail.configuration.metrics[].metricStat.metric.dimensions.
    We prefer that over parsing the alarm name string, which is fragile.
    """
    # Priority order for dimension keys
    dimension_keys = ("FunctionName", "QueueName", "TableName", "ApiName")

    configuration = detail.get("configuration", {})
    if isinstance(configuration, str):
        try:
            configuration = json.loads(configuration)
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.debug("[Handler] Could not parse configuration as JSON, using empty dict")
            configuration = {}
    if isinstance(configuration, dict):
        for metric in configuration.get("metrics", []) or []:
            if not isinstance(metric, dict):
                continue
            metric_stat = metric.get("metricStat", {})
            if not isinstance(metric_stat, dict):
                continue
            metric_inner = metric_stat.get("metric", {})
            if not isinstance(metric_inner, dict):
                continue
            dims = metric_inner.get("dimensions", {})
            if isinstance(dims, dict):
                for key in dimension_keys:
                    if key in dims:
                        logger.debug("[Handler] Resource from dimension %s=%s", key, dims[key])
                        return dims[key]
            elif isinstance(dims, list):
                dim_map = {d.get("name"): d.get("value") for d in dims if isinstance(d, dict) and "name" in d and "value" in d}
                for key in dimension_keys:
                    if key in dim_map:
                        logger.debug("[Handler] Resource from dimension %s=%s", key, dim_map[key])
                        return dim_map[key]

    # Fallback: strip the last known metric-name suffixes from alarm name
    metric_suffixes = (
        "-errors", "-throttles", "-timeout", "-oom",
        "-dlq-depth", "-dlq", "-5xx", "-throttle",
    )
    name = alarm_name
    for suffix in metric_suffixes:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    # Last resort: drop the last hyphen-segment
    parts = name.rsplit("-", 1)
    return parts[0] if len(parts) > 1 else name


def _extract_metrics_from_alarm(detail: dict) -> dict:
    """Extract metric values from the EventBridge alarm detail."""
    raw: dict = {}
    state = detail.get("state", {})
    raw["alarm_reason"] = state.get("reason", "")[:300] if isinstance(state, dict) else str(state)

    # Extract dimension values (e.g., FunctionName)
    configuration = detail.get("configuration", {})
    if isinstance(configuration, str):
        try:
            configuration = json.loads(configuration)
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.debug("[Handler] Could not parse configuration as JSON in _extract_metrics_from_alarm")
            configuration = {}
    if not isinstance(configuration, dict):
        configuration = {}

    metrics = configuration.get("metrics", [])
    if not isinstance(metrics, list):
        metrics = []

    for metric in metrics:
        if not isinstance(metric, dict):
            continue
        metric_stat = metric.get("metricStat", {})
        if not isinstance(metric_stat, dict):
            continue
        metric_inner = metric_stat.get("metric", {})
        if not isinstance(metric_inner, dict):
            continue
        dimensions = metric_inner.get("dimensions", [])
        if not isinstance(dimensions, list):
            continue
        for dim in dimensions:
            if isinstance(dim, dict) and "name" in dim and "value" in dim:
                raw[str(dim.get("name", ""))] = str(dim.get("value", ""))

    return raw
