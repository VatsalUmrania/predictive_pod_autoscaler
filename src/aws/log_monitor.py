"""
Log Monitor Lambda
===================
Triggered by CloudWatch Logs Subscription Filters.

Flow:
  Any /aws/lambda/* log group
      → Subscription Filter (pattern: ?ERROR ?Exception ?error)
      → THIS Lambda (log_monitor.handler)
      → log_analyst.analyze() → Bedrock LLM

  Branch A — predefined action (OOM, timeout, throttle, rollback):
      → remediation_agent.remediate() → AWS changes
      → Slack: ✅ Executed / ❌ Failed

  Branch B — alert_only with suggested_commands (IAM, missing queue, config):
      → approvals.create_approval() → DynamoDB
      → Slack: 🔵 Approval required — shows commands + approve URL

  Branch C — alert_only, no commands (low confidence, unknown error):
      → Slack: ⚪ Alert only — analysis + how_to_fix

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
from aws.agents.remediation_agent import remediate
from aws.config import AgentConfig
from aws.memory.approvals import create_approval
from aws.tools.aws_executor import validate_commands
from aws.tools.slack import send_log_error_alert

# force=True overrides Lambda's pre-configured root logger
logging.basicConfig(level=logging.INFO, force=True)
logger = logging.getLogger(__name__)


def handler(event: dict, context) -> dict:
    """
    Lambda entry point for CloudWatch Logs subscription events.

    Decodes the compressed log data, extracts error lines,
    calls the LLM for reasoning, attempts remediation if confident,
    and posts to Slack.
    """
    print("[LogMonitor] Handler invoked")

    try:
        cfg = AgentConfig.load()

        # Decode CloudWatch Logs subscription payload
        log_events = _decode_log_events(event)
        if not log_events:
            print("[LogMonitor] No log events in payload — skipping")
            return {"status": "no_events"}

        log_group  = log_events.get("logGroup", "unknown")
        log_stream = log_events.get("logStream", "unknown")
        events     = log_events.get("logEvents", [])

        # Extract the raw log messages
        messages = [e.get("message", "").strip() for e in events if e.get("message", "").strip()]

        if not messages:
            print(f"[LogMonitor] No messages after filtering in {log_group}")
            return {"status": "no_messages"}

        print(f"[LogMonitor] {len(messages)} error line(s) from {log_group}")
        for msg in messages[:3]:
            print(f"[LogMonitor] Sample: {msg[:200]}")

        # Infer function name from log group: /aws/lambda/<function-name>
        function_name = (
            log_group.replace("/aws/lambda/", "")
            if "/aws/lambda/" in log_group
            else log_group
        )

        # ── Step 1: LLM Analysis ──────────────────────────────────────────────
        print(f"[LogMonitor] Analyzing with model={cfg.bedrock_model}")
        analysis = analyze_logs(
            function_name=function_name,
            log_group=log_group,
            log_lines=messages,
            region=cfg.aws_region,
            cfg=cfg,
        )
        action     = analysis.get("action", "alert_only")
        confidence = float(analysis.get("confidence", 0.0))
        suggested_cmds = analysis.get("suggested_commands", [])
        print(
            f"[LogMonitor] action={action} confidence={confidence:.0%} "
            f"suggested_commands={len(suggested_cmds)}"
        )

        # ── Step 2: Route ─────────────────────────────────────────────────────
        remediation_result: dict | None = None
        approval_id: str | None = None

        if action != "alert_only":
            # Branch A: Predefined action — attempt auto-remediation
            print(f"[LogMonitor] Branch A: auto-remediate {action}")
            if function_name and "function_name" not in analysis.get("params", {}):
                analysis["params"]["function_name"] = function_name
            remediation_result = remediate(
                resource_name=function_name,
                rca=analysis,
                cfg=cfg,
            )
            approved = remediation_result.get("approved", False)
            success = (
                remediation_result.get("result", {}).get("success", False)
                if approved
                else False
            )
            print(f"[LogMonitor] Remediation: approved={approved} success={success}")

        elif suggested_cmds:
            # Branch B: alert_only but LLM generated specific commands → approval flow
            valid, reason = validate_commands(suggested_cmds)
            if valid:
                print(
                    f"[LogMonitor] Branch B: {len(suggested_cmds)} command(s) pending approval"
                )
                approval_id = create_approval(
                    commands=suggested_cmds,
                    function_name=function_name,
                    root_cause=analysis.get("root_cause", ""),
                    analysis=analysis,
                    region=cfg.aws_region,
                )
                print(f"[LogMonitor] Approval created: {approval_id}")
            else:
                print(f"[LogMonitor] Suggested commands failed validation: {reason}")
                suggested_cmds = []  # Fall through to Branch C

        else:
            # Branch C: alert_only, no actionable commands
            print("[LogMonitor] Branch C: alert only, no commands")

        # ── Step 3: Slack notification ────────────────────────────────────────
        api_base = os.environ.get("API_BASE_URL", "")
        slack_sent = send_log_error_alert(
            function_name=function_name,
            log_group=log_group,
            error_lines=messages,
            analysis=analysis,
            remediation_result=remediation_result,
            approval_id=approval_id,
            suggested_commands=suggested_cmds,
            api_base_url=api_base,
        )
        print(f"[LogMonitor] Slack sent={slack_sent}")

        return {
            "status":             "ok",
            "function_name":      function_name,
            "log_group":          log_group,
            "error_count":        len(messages),
            "action":             action,
            "confidence":         confidence,
            "approval_id":        approval_id,
            "slack_sent":         slack_sent,
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
        print(
            f"[LogMonitor] Decoded payload: "
            f"logGroup={decoded.get('logGroup')} "
            f"events={len(decoded.get('logEvents', []))}"
        )
        return decoded
    except Exception as exc:
        print(f"[LogMonitor] Failed to decode log payload: {exc}")
        return {}
