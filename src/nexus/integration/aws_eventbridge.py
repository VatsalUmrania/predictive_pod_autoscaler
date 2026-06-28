"""
NEXUS AWS EventBridge Webhook
==============================
FastAPI router that receives CloudWatch Alarm state change events
pushed via EventBridge → HTTP Target.

Instead of polling CloudWatch every 60 seconds, this endpoint lets AWS
push alarm state changes in near-real-time (< 5 seconds from alarm firing).

Setup in AWS (one-time):
    1. Create an EventBridge rule with event pattern:
       {
         "source": ["aws.cloudwatch"],
         "detail-type": ["CloudWatch Alarm State Change"],
         "detail": {"state": {"value": ["ALARM", "INSUFFICIENT_DATA"]}}
       }
    2. Add an HTTP Target pointing to:
       https://<your-nexus-endpoint>:8080/webhooks/aws/eventbridge
    3. Optionally configure a secret header for request authentication:
       Set NEXUS_EVENTBRIDGE_SECRET env var and add it as
       X-NEXUS-Secret header in the EventBridge HTTP target.

Payload schema (from AWS EventBridge):
    {
      "version": "0",
      "id": "<uuid>",
      "detail-type": "CloudWatch Alarm State Change",
      "source": "aws.cloudwatch",
      "account": "123456789012",
      "time": "2024-01-01T00:00:00Z",
      "region": "us-east-1",
      "detail": {
        "alarmName": "my-lambda-errors",
        "state": {
          "value": "ALARM",
          "reason": "...",
          "timestamp": "..."
        },
        "previousState": { "value": "OK", ... },
        "configuration": {
          "metrics": [...]
        }
      }
    }

Environment:
    NEXUS_EVENTBRIDGE_SECRET   Optional shared secret for request auth
                               (sent as X-NEXUS-Secret header from EventBridge)
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, status

logger = logging.getLogger(__name__)

# Optional shared secret for webhook authentication
_WEBHOOK_SECRET = os.getenv("NEXUS_EVENTBRIDGE_SECRET", "")

# Module-level NATS client reference (injected by NexusServer after startup)
_nats_client: Any = None


def set_nats_client(nats) -> None:
    """Called by NexusServer to inject the shared NATS client."""
    global _nats_client
    _nats_client = nats
    logger.info("[EventBridge] NATS client injected — webhook is active")


router = APIRouter(prefix="/webhooks/aws", tags=["aws-eventbridge"])


# ── Health check ──────────────────────────────────────────────────────────────

@router.get("/health")
async def eventbridge_health():
    """EventBridge webhook health check."""
    return {
        "status": "ok",
        "webhook": "/webhooks/aws/eventbridge",
        "nats_connected": _nats_client is not None,
        "auth_enabled": bool(_WEBHOOK_SECRET),
    }


# ── Main webhook ──────────────────────────────────────────────────────────────

@router.post("/eventbridge", status_code=status.HTTP_202_ACCEPTED)
async def receive_eventbridge_event(
    request: Request,
    background_tasks: BackgroundTasks,
    x_nexus_secret: str | None = Header(default=None, alias="X-NEXUS-Secret"),
):
    """
    Receive a CloudWatch Alarm state change from EventBridge.

    EventBridge sends HTTP POST with the alarm event JSON body.
    NEXUS converts this to an IncidentEvent and publishes to NATS,
    bypassing the polling loop for near-real-time response.
    """
    # ── Authentication ────────────────────────────────────────────────────────
    if _WEBHOOK_SECRET:
        if not x_nexus_secret:
            raise HTTPException(status_code=401, detail="Missing X-NEXUS-Secret header")
        if not hmac.compare_digest(x_nexus_secret, _WEBHOOK_SECRET):
            raise HTTPException(status_code=401, detail="Invalid X-NEXUS-Secret")

    # ── Parse body ────────────────────────────────────────────────────────────
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # ── Process in background ─────────────────────────────────────────────────
    background_tasks.add_task(_process_alarm_event, body)
    alarm_name = _extract_alarm_name(body)
    logger.info(f"[EventBridge] Received event for alarm: {alarm_name}")

    return {"status": "accepted", "alarm": alarm_name}


async def _process_alarm_event(event: dict) -> None:
    """
    Convert an EventBridge CloudWatch Alarm event to an IncidentEvent
    and publish to NATS.
    """
    from nexus.bus.incident_event import (
        AgentType, CloudWatchAlarmContext, IncidentEvent, Severity, SignalType
    )

    try:
        detail = event.get("detail", {})
        alarm_name = detail.get("alarmName", "unknown")
        state = detail.get("state", {})
        state_value = state.get("value", "ALARM")
        state_reason = state.get("reason", "")
        region = event.get("region", "us-east-1")
        alarm_arn = event.get("resources", [None])[0] if event.get("resources") else None

        # Only process ALARM and INSUFFICIENT_DATA states
        if state_value not in ("ALARM", "INSUFFICIENT_DATA"):
            logger.debug(f"[EventBridge] Ignoring non-alarm state '{state_value}' for {alarm_name}")
            return

        # Extract metric information from the configuration
        config = detail.get("configuration", {})
        metrics = config.get("metrics", [])
        metric_name = None
        namespace = None
        dimensions: dict = {}
        threshold = None
        comparison_operator = None

        if metrics:
            first_metric = metrics[0]
            metric_stat = first_metric.get("metricStat", {})
            metric = metric_stat.get("metric", {})
            metric_name = metric.get("metricName")
            namespace = metric.get("namespace")
            dimensions = {
                d.get("name", ""): d.get("value", "")
                for d in metric.get("dimensions", [])
            }
            threshold = config.get("description", {})
            comparison_operator = None  # Not in EventBridge payload directly

        severity = (
            Severity.CRITICAL
            if state_value == "ALARM"
            else Severity.WARNING
        )

        ctx = CloudWatchAlarmContext(
            alarm_name=alarm_name,
            alarm_arn=alarm_arn,
            metric_name=metric_name,
            namespace=namespace,
            state=state_value,
            state_reason=state_reason[:500] if state_reason else None,
            dimensions=dimensions,
        )

        event_obj = IncidentEvent(
            agent=AgentType.CLOUDWATCH,
            signal_type=SignalType.AWS_ALARM_FIRED,
            severity=severity,
            resource_name=alarm_name,
            confidence=0.90,   # High confidence — push from AWS, not polled
            context=ctx.model_dump(),
        )

        if _nats_client is not None:
            await _nats_client.publish(event_obj)
            logger.info(f"[EventBridge] Published IncidentEvent for alarm: {alarm_name} ({state_value})")
        else:
            logger.warning(
                f"[EventBridge] NATS client not available — "
                f"alarm {alarm_name} event dropped. "
                "Ensure NexusServer is fully started before receiving webhooks."
            )

    except Exception as exc:
        logger.error(f"[EventBridge] Error processing event: {exc}", exc_info=True)


def _extract_alarm_name(event: dict) -> str:
    """Extract alarm name from EventBridge payload for logging."""
    return (
        event.get("detail", {}).get("alarmName")
        or event.get("resources", ["unknown"])[0].split(":")[-1]
        or "unknown"
    )
