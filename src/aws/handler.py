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

# Configure logging at cold start
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Import the graph at cold start (compiled once, reused across invocations)
from aws.graph.workflow import run_graph

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


def lambda_handler(event: dict | str, context) -> dict:
    """
    AWS Lambda entry point.

    Parses the EventBridge alarm event, maps it to an incident dict,
    and runs the full LangGraph pipeline.
    """
    if isinstance(event, str):
        try:
            event = json.loads(event)
        except Exception as e:
            logger.error(f"[Handler] Failed to deserialize string event: {e}")
            return {"statusCode": 400, "body": f"Invalid JSON string event: {e}"}

    logger.info(f"[Handler] Received event: {json.dumps(event)[:500]}")

    # ── Parse the incoming event ──────────────────────────────────────────────
    try:
        incident = _parse_event(event)
    except Exception as exc:
        logger.error(f"[Handler] Failed to parse event: {exc}")
        return {"statusCode": 400, "body": f"Invalid event: {exc}"}

    # ── Ignore non-ALARM states ───────────────────────────────────────────────
    if incident is None:
        logger.info("[Handler] Event is not an ALARM state — skipping")
        return {"statusCode": 200, "body": "skipped"}

    logger.info(f"[Handler] Processing incident: {incident['resource_name']} / {incident['signal_type']}")

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
    logger.info(f"[Handler] Done: {summary}")
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
            "region":        event.get("region", os.getenv("DEFAULT_REGION", "us-east-1")),
        }

    # ── EventBridge CloudWatch Alarm event ───────────────────────────────────
    detail_type = event.get("detail-type", "")
    if detail_type != "CloudWatch Alarm State Change":
        logger.info(f"[Handler] Ignoring non-alarm event: {detail_type}")
        return None

    detail = event.get("detail", {})
    state_value = detail.get("state", {}).get("value", "")
    alarm_name = detail.get("alarmName", "unknown")
    region = event.get("region", os.getenv("DEFAULT_REGION", "us-east-1"))

    # Only respond to ALARM state (not OK or INSUFFICIENT_DATA)
    if state_value != "ALARM":
        logger.info(f"[Handler] Alarm '{alarm_name}' is in state '{state_value}' — ignoring")
        return None

    # Map alarm name to signal type
    signal_type = _map_alarm_to_signal(alarm_name)

    # Extract resource name from alarm name
    # Convention: "service-name-metric" → resource = "service-name"
    resource_name = _extract_resource_from_alarm(alarm_name)

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
    logger.info(f"[Handler] No signal mapping for alarm '{alarm_name}' — using aws_alarm_fired")
    return "aws_alarm_fired"


def _extract_resource_from_alarm(alarm_name: str) -> str:
    """
    Extract the resource name from the alarm name.

    Common conventions:
      "my-api-lambda-errors"      → "my-api-lambda"
      "payment-service-dlq-depth" → "payment-service"
    """
    # Try to extract from CloudWatch alarm dimensions in detail
    # (this is the reliable way; alarm name parsing is a fallback)
    parts = alarm_name.rsplit("-", 1)
    return parts[0] if len(parts) > 1 else alarm_name


def _extract_metrics_from_alarm(detail: dict) -> dict:
    """Extract metric values from the EventBridge alarm detail."""
    raw: dict = {}
    state = detail.get("state", {})
    raw["alarm_reason"] = state.get("reason", "")[:300]

    # Extract dimension values (e.g., FunctionName)
    for metric in detail.get("configuration", {}).get("metrics", []):
        for dim in metric.get("metricStat", {}).get("metric", {}).get("dimensions", []):
            raw[dim.get("name", "")] = dim.get("value", "")

    return raw
