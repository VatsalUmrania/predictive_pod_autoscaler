"""
Log Monitor Lambda
===================
Triggered by CloudWatch Logs Subscription Filters.

Flow:
  Any /aws/lambda/* log group
      → Subscription Filter (pattern: ?ERROR ?Exception ?error)
      → THIS Lambda (log_monitor.handler)
      → log_analyst.analyze() → Claude Sonnet
      → Slack notification with LLM reasoning

This is intentionally separate from the alarm-driven handler.py.
It catches errors that never cause Lambda failures (try/except'd errors,
AccessDeniedException logged as print(), etc.) — exactly the two errors
the user reported: QueueDoesNotExist and AccessDeniedException.

Event format from CloudWatch Logs subscription:
{
    "awslogs": {
        "data": "<base64-encoded gzip JSON>"
    }
}
"""

from __future__ import annotations

import base64
import gzip
import json
import logging
import os

from aws.agents.log_analyst import analyze_logs
from aws.tools.slack import send_log_error_alert

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def handler(event: dict, context) -> dict:
    """
    Lambda entry point for CloudWatch Logs subscription events.

    Decodes the compressed log data, extracts error lines,
    calls Claude for reasoning, and posts to Slack.
    """
    try:
        # Decode CloudWatch Logs subscription payload
        log_events = _decode_log_events(event)
        if not log_events:
            return {"status": "no_events"}

        log_group    = log_events.get("logGroup", "unknown")
        log_stream   = log_events.get("logStream", "unknown")
        events       = log_events.get("logEvents", [])
        region       = os.environ.get("DEFAULT_REGION", "us-east-1")

        # Extract the raw log messages
        messages = [e.get("message", "").strip() for e in events if e.get("message", "").strip()]

        if not messages:
            return {"status": "no_messages"}

        logger.info(
            f"[LogMonitor] Received {len(messages)} log lines from "
            f"{log_group} / {log_stream}"
        )

        # Infer function name from log group: /aws/lambda/<function-name>
        function_name = log_group.replace("/aws/lambda/", "") if "/aws/lambda/" in log_group else log_group

        # Call Claude to reason about the errors
        analysis = analyze_logs(
            function_name=function_name,
            log_group=log_group,
            log_lines=messages,
            region=region,
        )

        # Post to Slack regardless of confidence — user wants ALL errors notified
        slack_sent = send_log_error_alert(
            function_name=function_name,
            log_group=log_group,
            error_lines=messages,
            analysis=analysis,
        )

        logger.info(
            f"[LogMonitor] Analysis complete — "
            f"severity={analysis.get('severity')} "
            f"slack_sent={slack_sent}"
        )

        return {
            "status":        "ok",
            "function_name": function_name,
            "log_group":     log_group,
            "error_count":   len(messages),
            "severity":      analysis.get("severity"),
            "slack_sent":    slack_sent,
            "analysis":      analysis,
        }

    except Exception as exc:
        logger.error(f"[LogMonitor] Unhandled error: {exc}", exc_info=True)
        return {"status": "error", "error": str(exc)}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _decode_log_events(event: dict) -> dict:
    """
    CloudWatch Logs sends data as a base64-encoded gzip'd JSON blob.
    Returns the decoded dict or empty dict on failure.
    """
    try:
        awslogs = event.get("awslogs", {})
        data    = awslogs.get("data", "")
        if not data:
            return {}
        compressed = base64.b64decode(data)
        raw        = gzip.decompress(compressed)
        return json.loads(raw)
    except Exception as exc:
        logger.error(f"[LogMonitor] Failed to decode log payload: {exc}")
        return {}
