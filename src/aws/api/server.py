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
from mangum import Mangum

from aws.graph.workflow import run_graph

logger = logging.getLogger(__name__)

# Slack app credentials — set in Lambda environment variables (api.tf).
# SLACK_SIGNING_SECRET: Slack app → Basic Information → App Credentials
# SLACK_INTERACTIVE_URL: the agent_api_url Terraform output (Lambda Function URL)
#   used by slack.py when building button payloads; consumed here for docs only.
_SLACK_SIGNING_SECRET  = os.getenv("SLACK_SIGNING_SECRET", "")
_SLACK_INTERACTIVE_URL = os.getenv("SLACK_INTERACTIVE_URL", "")
# Reject interactive payloads whose timestamp is older than this (replay guard).
_SLACK_TOLERANCE_S = 60 * 5

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
    incident.setdefault("region", os.getenv("DEFAULT_REGION", "us-east-1"))

    result = run_graph(incident)
    return {
        "resource":   result.get("resource_name"),
        "action":     result.get("rca", {}).get("action"),
        "confidence": result.get("rca", {}).get("confidence"),
        "resolved": result.get("resolved"),
        "escalated": result.get("escalated"),
        "rca": result.get("rca"),
    }


@app.post("/approve/{approval_id}")
async def approve_commands(approval_id: str):
    """
    Execute a pending set of LLM-suggested commands after user approval.

    The user gets this URL from the Slack notification.
    Call it with: curl -X POST <api-url>/approve/<approval_id>

    Returns the execution results for each command.
    """
    from aws.config import AgentConfig
    from aws.memory.approvals import get_approval, update_status
    from aws.tools.aws_executor import execute_commands
    from aws.tools.slack import send_approval_result

    cfg = AgentConfig.load()

    # Fetch pending approval
    approval = get_approval(approval_id, region=cfg.aws_region)
    if not approval:
        raise HTTPException(
            status_code=404, detail=f"Approval '{approval_id}' not found or expired"
        )

    if approval.get("status") != "pending":
        return {
            "approval_id": approval_id,
            "status": approval.get("status"),
            "message": "This approval has already been processed",
        }

    commands = approval.get("commands", [])
    function_name = approval.get("function_name", "unknown")
    root_cause = approval.get("root_cause", "")

    logger.info(f"[API] Approving {len(commands)} command(s) for {function_name}")

    # Mark as approved (in-flight)
    update_status(approval_id, "approved", region=cfg.aws_region)

    # Execute
    results = execute_commands(commands, region=cfg.aws_region, dry_run=cfg.dry_run)
    all_ok = all(r.get("success") for r in results)
    final_status = "executed" if all_ok else "failed"
    update_status(approval_id, final_status, results=results, region=cfg.aws_region)

    # Notify Slack with results
    send_approval_result(
        function_name=function_name,
        approval_id=approval_id,
        root_cause=root_cause,
        results=results,
        all_ok=all_ok,
        dry_run=cfg.dry_run,
    )

    return {
        "approval_id": approval_id,
        "status": final_status,
        "function": function_name,
        "commands_run": len(results),
        "all_ok": all_ok,
        "results": results,
    }


@app.post("/reject/{approval_id}")
async def reject_commands(approval_id: str):
    """Mark a pending approval as rejected (no execution)."""
    from aws.config import AgentConfig
    from aws.memory.approvals import get_approval, update_status

    cfg = AgentConfig.load()
    approval = get_approval(approval_id, region=cfg.aws_region)
    if not approval:
        raise HTTPException(
            status_code=404, detail=f"Approval '{approval_id}' not found or expired"
        )

    update_status(approval_id, "rejected", region=cfg.aws_region)
    return {"approval_id": approval_id, "status": "rejected"}


@app.post("/slack/interactive")
async def slack_interactive(request: Request):
    """
    Handle Slack interactive button clicks (Approve ✅ / Reject ❌).

    Slack POSTs ``application/x-www-form-urlencoded`` with a ``payload`` field
    containing JSON whenever a user clicks an action button in a Block Kit message.

    Flow:
      1. Verify the Slack HMAC-SHA256 request signature (``v0`` scheme).
      2. Parse the ``payload`` JSON to extract ``action_id`` and ``value`` (approval_id).
      3. Route to the existing approve / reject logic.
      4. Return ``{"replace_original": true, "text": …}`` so Slack replaces the
         buttons with the decision — preventing a second operator from acting on
         an already-resolved approval.

    Requires env vars:
      SLACK_SIGNING_SECRET  — Slack app → Basic Information → App Credentials
      SLACK_INTERACTIVE_URL — this Lambda Function URL (agent_api_url Terraform output)
    """
    import urllib.parse

    from aws.config import AgentConfig
    from aws.memory.approvals import get_approval, update_status
    from aws.tools.slack import send_approval_result

    # ── 1. Signature verification —————————————————————————─
    raw = await request.body()
    if not _verify_slack_request(raw, request.headers):
        raise HTTPException(status_code=401, detail="invalid_signature")

    # ── 2. Parse payload ———————————————————————————─
    try:
        qs = urllib.parse.parse_qs(raw.decode("utf-8"))
        payload_str = (qs.get("payload") or [None])[0]
        data = json.loads(payload_str)
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed interactive payload")

    try:
        action    = data["actions"][0]
        action_id = action["action_id"]   # "nexus_approve" | "nexus_reject"
        approval_id = action["value"]     # UUID stored in the button's value field
        user = (
            data.get("user", {}).get("username")
            or data.get("user", {}).get("id")
            or "unknown"
        )
    except (KeyError, IndexError, TypeError):
        raise HTTPException(status_code=400, detail="Missing action fields in payload")

    # ── 3. Lookup approval ————————————————————————─
    cfg = AgentConfig.load()
    approval = get_approval(approval_id, region=cfg.aws_region)
    if not approval:
        return {
            "replace_original": True,
            "text": f"⚠️ Approval `{approval_id}` not found or has expired.",
        }

    if approval.get("status") != "pending":
        status = approval.get("status", "unknown")
        icon = "✅" if status == "executed" else ("❌" if status == "rejected" else "⏳")
        return {
            "replace_original": True,
            "text": f"{icon} This approval has already been *{status}* — no further action taken.",
        }

    function_name = approval.get("function_name", "unknown")
    root_cause    = approval.get("root_cause", "")

    # ── 4a. Reject path —————————————————————————─
    if action_id == "nexus_reject":
        update_status(approval_id, "rejected", region=cfg.aws_region)
        logger.info(f"[Slack] ❌ Rejected by @{user}: approval_id={approval_id}")
        return {
            "replace_original": True,
            "text": f"❌ Rejected `{approval_id}` by @{user} — no commands will run.",
        }

    # ── 4b. Approve path ———————————————————————─
    if action_id == "nexus_approve":
        from aws.tools.aws_executor import execute_commands

        commands = approval.get("commands", [])
        logger.info(f"[Slack] ✅ Approved by @{user}: approval_id={approval_id}, commands={len(commands)}")

        update_status(approval_id, "approved", region=cfg.aws_region)
        results   = execute_commands(commands, region=cfg.aws_region, dry_run=cfg.dry_run)
        all_ok    = all(r.get("success") for r in results)
        final_status = "executed" if all_ok else "failed"
        update_status(approval_id, final_status, results=results, region=cfg.aws_region)

        send_approval_result(
            function_name=function_name,
            approval_id=approval_id,
            root_cause=root_cause,
            results=results,
            all_ok=all_ok,
            dry_run=cfg.dry_run,
        )

        icon = "✅" if all_ok else "❌"
        outcome = "succeeded" if all_ok else "partially failed"
        return {
            "replace_original": True,
            "text": (
                f"{icon} Approved `{approval_id}` by @{user} — "
                f"{len(results)} command(s) {outcome}. Check the next Slack message for details."
            ),
        }

    # Unknown action_id — safe fallback
    raise HTTPException(status_code=400, detail=f"Unknown action_id: {action_id}")


# ── Lambda handler via Mangum ─────────────────────────────────────────────────
# Used when deploying as a Lambda function with API Gateway / Function URL
handler = Mangum(app, lifespan="off")


# ── Internal helpers ──────────────────────────────────────────────────────────

def _verify_slack_request(raw_body: bytes, headers) -> bool:
    """Validate a Slack request using the ``v0`` HMAC-SHA256 signature scheme.

    Returns False for ANY failure (missing secret, missing headers, stale
    timestamp, or signature mismatch) so callers can treat it as a hard auth
    gate without catching exceptions.

    Ref: https://api.slack.com/authentication/verifying-requests-from-slack
    """
    import hashlib
    import hmac as _hmac

    if not _SLACK_SIGNING_SECRET:
        logger.warning("[Slack] SLACK_SIGNING_SECRET not set — rejecting all interactive requests")
        return False

    ts  = headers.get("X-Slack-Request-Timestamp")
    sig = headers.get("X-Slack-Signature")
    if not ts or not sig:
        return False

    try:
        ts_int = int(ts)
    except (TypeError, ValueError):
        return False

    # Replay guard: reject requests older than 5 minutes
    from datetime import datetime, timezone
    now = int(datetime.now(timezone.utc).timestamp())
    if abs(now - ts_int) > _SLACK_TOLERANCE_S:
        return False

    base     = b"v0:" + str(ts).encode() + b":" + raw_body
    expected = "v0=" + _hmac.new(
        _SLACK_SIGNING_SECRET.encode("utf-8"), base, hashlib.sha256
    ).hexdigest()
    return _hmac.compare_digest(expected, str(sig))


def _parse_eventbridge(event: dict[str, Any]) -> dict[str, Any]:
    """Parse EventBridge CloudWatch Alarm event into incident dict."""
    detail = event.get("detail", {})
    alarm_name = detail.get("alarmName", "unknown")
    region = event.get("region", os.getenv("DEFAULT_REGION", "us-east-1"))
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
