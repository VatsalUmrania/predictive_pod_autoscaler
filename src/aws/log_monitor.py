"""
Log Monitor Lambda
===================
Triggered by CloudWatch Logs Subscription Filters.

Flow:
  Any /aws/lambda/* log group
      → Subscription Filter (pattern: ?ERROR ?Exception ?error)
      → THIS Lambda (log_monitor.handler)
      → log_analyst.analyze() → Bedrock LLM
      → Slack notification with LLM reasoning

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

# force=True overrides Lambda's pre-configured root logger
logging.basicConfig(level=logging.INFO, force=True)
logger = logging.getLogger(__name__)


def handler(event: dict, context) -> dict:
    """
    Lambda entry point for CloudWatch Logs subscription events.

    Decodes the compressed log data, extracts error lines,
    calls the LLM for reasoning, and posts to Slack.
    """
    print("[LogMonitor] Handler invoked")

    try:
        # Decode CloudWatch Logs subscription payload
        log_events = _decode_log_events(event)
        if not log_events:
            print("[LogMonitor] No log events in payload — skipping")
            return {"status": "no_events"}

        log_group    = log_events.get("logGroup", "unknown")
        log_stream   = log_events.get("logStream", "unknown")
        events       = log_events.get("logEvents", [])
        region       = os.environ.get("DEFAULT_REGION", "us-east-1")

        # Extract the raw log messages
        messages = [e.get("message", "").strip() for e in events if e.get("message", "").strip()]

        if not messages:
            print(f"[LogMonitor] Log group {log_group} triggered but no messages after filtering")
            return {"status": "no_messages"}

        print(f"[LogMonitor] {len(messages)} error line(s) from {log_group}")
        for msg in messages[:3]:
            print(f"[LogMonitor] Sample: {msg[:200]}")

        # Infer function name from log group: /aws/lambda/<function-name>
        function_name = log_group.replace("/aws/lambda/", "") if "/aws/lambda/" in log_group else log_group

        # Call LLM to reason about the errors
        print(f"[LogMonitor] Calling Bedrock for analysis (model={os.environ.get('AGENT_BEDROCK_MODEL', 'unset')})")
        analysis = analyze_logs(
            function_name=function_name,
            log_group=log_group,
            log_lines=messages,
            region=region,
        )
        print(f"[LogMonitor] Analysis done — severity={analysis.get('severity')} root_cause={analysis.get('root_cause', '')[:80]}")

        # Post to Slack
        slack_sent = send_log_error_alert(
            function_name=function_name,
            log_group=log_group,
            error_lines=messages,
            analysis=analysis,
        )
        print(f"[LogMonitor] Slack sent={slack_sent}")

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
        print(f"[LogMonitor] UNHANDLED ERROR: {exc}")
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
            print("[LogMonitor] Event has no awslogs.data field")
            return {}
        compressed = base64.b64decode(data)
        raw        = gzip.decompress(compressed)
        decoded    = json.loads(raw)
        print(f"[LogMonitor] Decoded payload: logGroup={decoded.get('logGroup')} events={len(decoded.get('logEvents', []))}")
        return decoded
    except Exception as exc:
        print(f"[LogMonitor] Failed to decode log payload: {exc}")
        logger.error(f"[LogMonitor] Failed to decode log payload: {exc}")
        return {}
