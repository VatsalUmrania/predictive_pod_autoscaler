"""
Slack Notification Tools
=========================
Sends messages to Slack via Incoming Webhooks.
Uses httpx for async-compatible HTTP (sync calls are fine in Lambda).

Set SLACK_WEBHOOK_URL in environment variables.
Get your webhook URL from: https://api.slack.com/messaging/webhooks
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
# Public base URL of the agent API (Lambda Function URL from Terraform output
# `agent_api_url`). When set, Slack interactive-component buttons POST back to
# <SLACK_INTERACTIVE_URL>/slack/interactive.  Mirrors SLACK_INTERACTIVE_URL in
# nexus/integration/notifier.py — one env var, two notification paths.
_SLACK_INTERACTIVE_URL = os.getenv("SLACK_INTERACTIVE_URL", "")


def send_alert(
    message: str,
    title: str = "NEXUS AI DevOps Agent",
    color: str = "#FF0000",    # Red for alerts
    fields: list[dict] | None = None,
    webhook_url: str = "",
) -> bool:
    """
    Send a formatted Slack alert.

    Args:
        message:     Main message text
        title:       Block title
        color:       Attachment sidebar color (hex)
        fields:      Optional list of {"title": ..., "value": ..., "short": bool}
        webhook_url: Override SLACK_WEBHOOK_URL env var

    Returns True if sent successfully, False on error.
    """
    url = webhook_url or _SLACK_WEBHOOK_URL
    if not url:
        logger.warning("[Slack] SLACK_WEBHOOK_URL not set — alert not sent")
        return False

    payload = {
        "attachments": [
            {
                "color": color,
                "title": title,
                "text": message,
                "fields": fields or [],
                "footer": "NEXUS AI DevOps Agent",
                "ts": _now_ts(),
            }
        ]
    }

    return _post_to_slack(url, payload)


def send_incident_alert(
    resource_name: str,
    signal_type: str,
    root_cause: str,
    action_taken: str,
    confidence: float,
    resolved: bool,
    webhook_url: str = "",
) -> bool:
    """
    Send a structured incident notification to Slack.
    Used after remediation to report the outcome.
    """
    icon = "✅" if resolved else "🔴"
    color = "#36a64f" if resolved else "#FF0000"
    status = "RESOLVED" if resolved else "UNRESOLVED — Manual action required"

    fields = [
        {"title": "Resource", "value": f"`{resource_name}`", "short": True},
        {"title": "Signal", "value": signal_type, "short": True},
        {"title": "Root Cause", "value": root_cause, "short": False},
        {"title": "Action Taken", "value": action_taken, "short": True},
        {"title": "Confidence", "value": f"{confidence:.0%}", "short": True},
        {"title": "Status", "value": status, "short": False},
    ]

    return send_alert(
        message=f"{icon} *{status}*",
        title="NEXUS Incident Report",
        color=color,
        fields=fields,
        webhook_url=webhook_url,
    )


def send_approval_request(
    resource_name: str,
    action: str,
    params: dict[str, Any],
    root_cause: str,
    confidence: float,
    webhook_url: str = "",
) -> bool:
    """
    Send a Slack message requesting human approval for a low-confidence action.

    NOTE: Full approval/reject buttons require Slack App with interactive components.
    For now, this sends a clear alert asking for manual review.
    A future version can use Slack Block Kit actions with a callback URL.
    """
    url = webhook_url or _SLACK_WEBHOOK_URL
    if not url:
        logger.warning("[Slack] SLACK_WEBHOOK_URL not set — approval request not sent")
        return False

    fields = [
        {"title": "Resource", "value": f"`{resource_name}`", "short": True},
        {"title": "Proposed Action", "value": action, "short": True},
        {"title": "Parameters", "value": f"```{json.dumps(params, indent=2)}```", "short": False},
        {"title": "Root Cause", "value": root_cause, "short": False},
        {"title": "Confidence", "value": f"{confidence:.0%} (below auto-execute threshold)", "short": False},
    ]

    payload = {
        "attachments": [
            {
                "color": "#FFA500",     # Orange for approval requests
                "title": "⚠️ NEXUS: Human Approval Required",
                "text": (
                    "NEXUS has diagnosed an incident but confidence is below the auto-execute threshold.\n"
                    "*Please review and take manual action if appropriate.*"
                ),
                "fields": fields,
                "footer": "NEXUS AI DevOps Agent | Auto-execute threshold: 85%",
                "ts": _now_ts(),
            }
        ]
    }

    return _post_to_slack(url, payload)


def _approval_buttons_block(approval_id: str) -> dict:
    """Return a Slack Block Kit ``actions`` block with Approve ✅ / Reject ❌ buttons.

    The buttons POST to ``<SLACK_INTERACTIVE_URL>/slack/interactive`` which is
    handled by the ``POST /slack/interactive`` endpoint in server.py.
    When ``SLACK_INTERACTIVE_URL`` is not set the block degrades silently to an
    empty context block so the rest of the message still renders.
    """
    if not _SLACK_INTERACTIVE_URL:
        return {"type": "context", "elements": [{"type": "mrkdwn", "text": ""}]}
    return {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Approve ✅", "emoji": True},
                "style": "primary",
                "action_id": "nexus_approve",
                "value": approval_id,
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Reject ❌", "emoji": True},
                "style": "danger",
                "action_id": "nexus_reject",
                "value": approval_id,
            },
        ],
    }


def send_log_error_alert(
    function_name: str,
    log_group: str,
    error_lines: list[str],
    analysis: dict[str, Any],
    remediation_result: dict | None = None,
    approval_id: str | None = None,
    suggested_commands: list[dict] | None = None,
    api_base_url: str = "",
    webhook_url: str = "",
) -> bool:
    """
    Send a Slack alert with raw error log lines + LLM analysis + remediation outcome.
    This is the primary notification function for the log monitor pipeline.

    Format:
      🔴 ERROR DETECTED in <function_name>
      ─────────────────────────────────
      Severity: High
      Root Cause: IAM permission missing — dynamodb:PutItem not allowed
      What it means: Writes to DynamoDB are failing silently
      How to fix:
        1. Add dynamodb:PutItem to the Lambda execution role
        2. Re-apply Terraform: terraform apply
      ─────────────────────────────────
      Raw log lines (last 5):
        [ERROR] AccessDeniedException: ...
    """
    url = webhook_url or _SLACK_WEBHOOK_URL
    if not url:
        logger.warning("[Slack] SLACK_WEBHOOK_URL not set — log error alert not sent")
        return False

    severity       = analysis.get("severity", "Unknown")
    root_cause     = analysis.get("root_cause", "Unable to determine")
    what_it_means  = analysis.get("what_it_means", "")
    how_to_fix     = analysis.get("how_to_fix", "Review the logs manually.")
    is_infra_issue = analysis.get("is_infra_issue", False)
    error_types    = analysis.get("error_types", [])
    action         = analysis.get("action", "alert_only")
    confidence     = float(analysis.get("confidence", 0.0))

    # Color by severity
    color_map = {
        "Critical": "#8B0000",
        "High": "#FF0000",
        "Medium": "#FFA500",
        "Low":  "#FFFF00",
        "Unknown": "#808080",
    }
    color = color_map.get(severity, "#808080")

    severity_emoji = {
        "Critical": "💀", "High": "🔴", "Medium": "🟡", "Low": "🟢", "Unknown": "⚪",
    }.get(severity, "⚪")

    infra_tag = " 🏗️ *Infrastructure Issue*" if is_infra_issue else ""

    # Trim log lines for the Slack message
    shown_lines = error_lines[-5:]
    log_block = "\n".join(f"  {line[:200]}" for line in shown_lines)

    fields = [
        {"title": "Function",   "value": f"`{function_name}`",   "short": True},
        {"title": "Log Group",  "value": f"`{log_group}`",       "short": True},
        {"title": "Severity",   "value": f"{severity_emoji} {severity}{infra_tag}", "short": True},
        {"title": "Error Count", "value": str(len(error_lines)), "short": True},
        {"title": "Root Cause", "value": root_cause,             "short": False},
        {"title": "What It Means", "value": what_it_means,       "short": False},
        {"title": "How To Fix",    "value": how_to_fix,          "short": False},
    ]

    # Add error types if available
    if error_types:
        fields.append({"title": "Error Types", "value": ", ".join(f"`{e}`" for e in error_types), "short": False})

    # Build the Slack attachments
    attachments = [
        {
            "color":   color,
            "title":   f"{severity_emoji} ERROR DETECTED in `{function_name}`",
            "pretext": "NEXUS AI DevOps Agent detected errors in CloudWatch Logs",
            "fields":  fields,
            "footer":  "NEXUS Log Monitor | CloudWatch Logs Subscription",
            "ts":      _now_ts(),
        },
        {
            "color":     "#333333",
            "title":     f"Raw Log Lines (last {len(shown_lines)} of {len(error_lines)})",
            "text":      f"```{log_block}```",
            "mrkdwn_in": ["text"],
        },
    ]

    # Remediation block — appended when an action was attempted
    if remediation_result is not None:
        approved = remediation_result.get("approved", False)
        result   = remediation_result.get("result") or {}
        success  = result.get("success", False)
        dry_run  = result.get("dry_run", False)

        if not approved:
            rem_color  = "#FFA500"   # orange — blocked
            rem_title  = f"⛔ Remediation BLOCKED"
            rem_text   = f"*Action:* `{action}` | *Reason:* {remediation_result.get('reason', '')}"
        elif dry_run:
            rem_color  = "#0099CC"   # blue — dry-run
            rem_title  = f"🔵 Remediation DRY-RUN (no changes made)"
            rem_text   = f"*Action:* `{action}` | *Confidence:* {confidence:.0%}"
        elif success:
            rem_color  = "#36a64f"   # green — executed
            rem_title  = f"✅ Remediation EXECUTED"
            pre  = result.get("pre", {})
            post = result.get("post", {})
            rem_text   = (
                f"*Action:* `{action}` | *Confidence:* {confidence:.0%}\n"
                f"*Before:* `{pre}` → *After:* `{post}`"
            )
        else:
            rem_color  = "#FF0000"   # red — failed
            rem_title  = f"❌ Remediation FAILED"
            rem_text   = (
                f"*Action:* `{action}` | *Error:* {result.get('error', 'unknown')}"
            )

        attachments.append(
            {
                "color": rem_color,
                "title": rem_title,
                "text": rem_text,
                "mrkdwn_in": ["text"],
            }
        )

    # Approval block — shown when LLM suggested commands pending human review
    if approval_id and suggested_commands:
        cmd_lines = []
        for i, cmd in enumerate(suggested_commands, 1):
            desc = cmd.get("description", f"Command {i}")
            svc = cmd.get("service", "")
            method = cmd.get("method", "")
            risk = cmd.get("risk", "medium")
            cmd_lines.append(f"{i}. *{desc}* — `{svc}.{method}` _(risk: {risk})_")

        # Plain-text fallback (older clients / when buttons are disabled)
        base = api_base_url or _SLACK_INTERACTIVE_URL.rstrip("/")
        approve_cmd = (
            f"`curl -X POST {base}/approve/{approval_id}`"
            if base
            else f"Call `POST /approve/{approval_id}` on your API Gateway URL"
        )
        reject_cmd = (
            f"`curl -X POST {base}/reject/{approval_id}`"
            if base
            else f"Call `POST /reject/{approval_id}` to discard"
        )
        fallback_text = (
            f"NEXUS approval required for {len(suggested_commands)} command(s) on `{function_name}`. "
            f"Approve: {approve_cmd}  Reject: {reject_cmd}"
        )

        # Build the Block Kit blocks for the interactive message
        blocks: list[dict] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"🔵 Approval Required — {len(suggested_commands)} command(s)",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Function:* `{function_name}`\n"
                        "*LLM-suggested fixes (not yet executed):*\n"
                        + "\n".join(cmd_lines)
                    ),
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Approval ID: `{approval_id}` · Expires in 24 h",
                    }
                ],
            },
            {"type": "divider"},
            _approval_buttons_block(approval_id),
        ]

        attachments.append(
            {
                "color":    "#0099CC",
                "fallback": fallback_text,
                "blocks":   blocks,
            }
        )

    return _post_to_slack(url, {"attachments": attachments})


def send_approval_result(
    function_name: str,
    approval_id: str,
    root_cause: str,
    results: list[dict[str, Any]],
    all_ok: bool,
    dry_run: bool = False,
    webhook_url: str = "",
) -> bool:
    """
    Post the outcome of an approved command execution to Slack.
    Called by the /approve/{id} API endpoint after running the commands.
    """
    url = webhook_url or _SLACK_WEBHOOK_URL
    if not url:
        return False

    icon = "✅" if all_ok else "❌"
    color = "#36a64f" if all_ok else "#FF0000"
    title = (
        f"{icon} Commands {'DRY-RUN' if dry_run else 'Executed'} for `{function_name}`"
    )

    lines = [f"*Approval ID:* `{approval_id}`", f"*Root Cause:* {root_cause}", ""]
    for i, r in enumerate(results, 1):
        status = "✅" if r.get("success") else "❌"
        desc = r.get("description", f"Command {i}")
        svc = r.get("service", "")
        method = r.get("method", "")
        err = r.get("error", "")
        lines.append(f"{status} *{i}. {desc}*")
        lines.append(f"   `{svc}.{method}`")
        if err:
            lines.append(f"   Error: {err}")

    payload = {
        "attachments": [
            {
                "color": color,
                "title": title,
                "text": "\n".join(lines),
                "mrkdwn_in": ["text"],
                "footer": "NEXUS AI DevOps Agent | Approval Execution",
                "ts": _now_ts(),
            }
        ]
    }
    return _post_to_slack(url, payload)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _post_to_slack(url: str, payload: dict) -> bool:
    """POST payload to Slack webhook URL. Returns True on 200."""
    try:
        import urllib.request
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            ok = resp.status == 200
            if not ok:
                logger.warning(f"[Slack] Unexpected status {resp.status}")
            return ok
    except Exception as exc:
        logger.error(f"[Slack] Failed to send message: {exc}")
        return False


def _now_ts() -> int:
    from datetime import datetime, timezone
    return int(datetime.now(timezone.utc).timestamp())
