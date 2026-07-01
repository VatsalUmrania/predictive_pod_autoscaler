"""
FastAPI Server — Webhook Entry Point
======================================
Receives AWS EventBridge alarm events and triggers the AI agent pipeline.

Endpoints:
  POST /webhook/eventbridge   ← EventBridge sends CloudWatch Alarm events here
  GET  /health                ← Load balancer / ECS health check
  GET  /status                ← Last incident summary

Deploy on Lambda with Mangum adapter, or as a container on ECS Fargate.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from mangum import Mangum

from aws.graph.workflow import run_graph

logger = logging.getLogger(__name__)

app = FastAPI(
    title="NEXUS AI DevOps Agent",
    description="Autonomous AI DevOps engineer for AWS serverless infrastructure",
    version="1.0.0",
)

# Store last incident summary in memory (last 10 incidents)
_recent_incidents: list[dict] = []


@app.get("/health")
def health_check():
    """Load balancer / ECS health check endpoint."""
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/status")
def get_status():
    """Return recent incident summaries."""
    return {
        "agent": "NEXUS AI DevOps Agent",
        "recent_incidents": _recent_incidents[-10:],
        "total_processed": len(_recent_incidents),
    }


@app.post("/webhook/eventbridge")
async def eventbridge_webhook(request: Request):
    """
    Receive CloudWatch Alarm state change events from AWS EventBridge.

    EventBridge must be configured to send to this URL.
    AWS will include an SNS-style confirmation for new subscriptions.
    """
    body = await request.body()
    try:
        event = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Handle SNS subscription confirmation (one-time)
    if event.get("Type") == "SubscriptionConfirmation":
        subscribe_url = event.get("SubscribeURL", "")
        logger.info(f"[Webhook] SNS subscription confirmation — visit: {subscribe_url}")
        return {"message": "Subscription confirmation received"}

    # Handle actual alarm event
    detail_type = event.get("detail-type", "")
    if detail_type != "CloudWatch Alarm State Change":
        return {"message": f"Ignored event type: {detail_type}"}

    detail = event.get("detail", {})
    state = detail.get("state", {}).get("value", "")
    alarm_name = detail.get("alarmName", "unknown")

    if state != "ALARM":
        return {"message": f"Alarm '{alarm_name}' is {state} — not processing"}

    # Parse into incident dict and run the graph
    incident = _parse_eventbridge(event)
    logger.info(f"[Webhook] Processing alarm: {alarm_name}")

    result = run_graph(incident)

    summary = {
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "alarm":       alarm_name,
        "resource":    result.get("resource_name"),
        "action":      result.get("rca", {}).get("action"),
        "resolved":    result.get("resolved"),
        "escalated":   result.get("escalated"),
    }
    _recent_incidents.append(summary)

    return summary


@app.post("/test/incident")
async def test_incident(request: Request):
    """
    Directly inject a test incident without going through EventBridge.
    Useful for local testing.

    Body: {"resource_name": "my-lambda", "signal_type": "lambda_error_rate_high"}
    """
    body = await request.body()
    try:
        incident = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if "resource_name" not in incident:
        raise HTTPException(status_code=400, detail="resource_name is required")

    incident.setdefault("signal_type", "lambda_error_rate_high")
    incident.setdefault("raw_metrics", {})
    incident.setdefault("region", os.getenv("AWS_DEFAULT_REGION", "us-east-1"))

    result = run_graph(incident)
    return {
        "resource":   result.get("resource_name"),
        "action":     result.get("rca", {}).get("action"),
        "confidence": result.get("rca", {}).get("confidence"),
        "resolved":   result.get("resolved"),
        "escalated":  result.get("escalated"),
        "rca":        result.get("rca"),
    }


# ── Lambda handler via Mangum ─────────────────────────────────────────────────
# Used when deploying as a Lambda function with API Gateway / Function URL
handler = Mangum(app, lifespan="off")


# ── Internal helpers ──────────────────────────────────────────────────────────

def _parse_eventbridge(event: dict[str, Any]) -> dict[str, Any]:
    """Parse EventBridge CloudWatch Alarm event into incident dict."""
    detail = event.get("detail", {})
    alarm_name = detail.get("alarmName", "unknown")
    region = event.get("region", os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
    reason = detail.get("state", {}).get("reason", "")

    # Extract resource name from dimensions
    resource_name = alarm_name
    for metric in detail.get("configuration", {}).get("metrics", []):
        for dim in metric.get("metricStat", {}).get("metric", {}).get("dimensions", []):
            if dim.get("name") in ("FunctionName", "QueueName", "TableName"):
                resource_name = dim.get("value", alarm_name)
                break

    return {
        "resource_name": resource_name,
        "signal_type":   _map_alarm(alarm_name),
        "raw_metrics":   {"alarm_reason": reason[:300]},
        "region":        region,
        "alarm_name":    alarm_name,
        "alarm_reason":  reason,
    }


_ALARM_MAP = {
    "lambda-errors":    "lambda_error_rate_high",
    "lambda-throttles": "lambda_throttle_spike",
    "lambda-timeout":   "lambda_timeout",
    "sqs-dlq":          "sqs_dlq_depth_high",
    "dynamo-throttle":  "dynamo_throttle_write",
    "apigw-5xx":        "apigw_5xx_spike",
}

def _map_alarm(alarm_name: str) -> str:
    alarm_lower = alarm_name.lower()
    for pattern, signal in _ALARM_MAP.items():
        if pattern in alarm_lower:
            return signal
    return "aws_alarm_fired"
