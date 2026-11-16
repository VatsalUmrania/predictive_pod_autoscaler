"""Incident and report Slack Block Kit templates for Notifier."""

from __future__ import annotations

from typing import Any


def build_incident_resolved_payload(
    target: str,
    incident_id: str,
    failure_class: str,
    confidence: float,
    duration_s: float | None,
    root_cause: str,
    action_taken: str,
    verification_details: str,
) -> dict[str, Any]:
    """Build Slack Block Kit payload for resolved incident."""
    conf_str = f"{confidence * 100:.0f}%" if confidence <= 1.0 else f"{confidence:.0f}%"
    dur_str = f" in {duration_s:.1f}s" if duration_s is not None else ""
    fallback = f"✅ NEXUS Incident Resolved for {target}: {failure_class}"

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"✅ NEXUS Incident Resolved — {target}",
                "emoji": True,
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Incident ID:*\n`{incident_id}`"},
                {"type": "mrkdwn", "text": f"*Target:*\n`{target}`"},
                {"type": "mrkdwn", "text": f"*Failure Class:*\n`{failure_class}`"},
                {"type": "mrkdwn", "text": f"*Confidence:*\n`{conf_str}`"},
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Root Cause Analysis:*\n{root_cause[:350]}{'...' if len(root_cause) > 350 else ''}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Action Executed:*\n{action_taken[:350]}{'...' if len(action_taken) > 350 else ''}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Health & SLO Verification:*\n`{verification_details[:350]}`",
            },
        },
        {"type": "divider"},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"🤖 *NEXUS Autonomous Healing* • Resolved{dur_str} • Verified healthy",
                }
            ],
        },
    ]

    return {
        "text": fallback,
        "attachments": [
            {
                "color": "#059669",
                "fallback": fallback,
                "blocks": blocks,
            }
        ],
    }


def build_incident_escalated_payload(
    target: str,
    incident_id: str,
    failure_class: str,
    reason: str,
    root_cause: str,
) -> dict[str, Any]:
    """Build Slack Block Kit payload for escalated incident."""
    fallback = f"🚨 NEXUS Incident Escalated for {target}: {reason}"
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🚨 NEXUS Incident Escalation — {target}",
                "emoji": True,
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Incident ID:*\n`{incident_id}`"},
                {"type": "mrkdwn", "text": f"*Target:*\n`{target}`"},
                {"type": "mrkdwn", "text": f"*Failure Class:*\n`{failure_class}`"},
                {"type": "mrkdwn", "text": "*Status:*\n`ESCALATED`"},
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Escalation Reason:*\n{reason[:350]}"},
        },
    ]
    if root_cause and root_cause != "Unknown":
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Suspected Cause:*\n{root_cause[:350]}{'...' if len(root_cause) > 350 else ''}",
                },
            }
        )
    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "⚠️ *Requires Human Intervention* • Escalated to on-call engineering team",
                }
            ],
        }
    )
    return {
        "text": fallback,
        "attachments": [
            {
                "color": "#dc2626",
                "fallback": fallback,
                "blocks": blocks,
            }
        ],
    }


def build_report_ready_payload(
    target: str,
    outcome: str,
    summary: str,
    report_url: str,
) -> dict[str, Any]:
    """Build Slack Block Kit payload for post-incident report."""
    fallback = f"NEXUS post-incident report ready for {target} ({outcome})"
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"📄 NEXUS Post-Incident Report — {target}",
                "emoji": True,
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Target:*\n`{target}`"},
                {"type": "mrkdwn", "text": f"*Outcome:*\n`{outcome.upper()}`"},
            ],
        },
    ]
    if summary:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Summary:*\n{summary[:600]}{'...' if len(summary) > 600 else ''}",
                },
            }
        )
    if report_url:
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Detailed Report:*\n<{report_url}|Open full report>",
                },
                "accessory": {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "📊 Open Full Report",
                        "emoji": True,
                    },
                    "url": report_url,
                    "style": "primary",
                    "action_id": "open_report_link",
                },
            }
        )
    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"🤖 *NEXUS Observability Engine* • Report Generated • Target: `{target}`",
                }
            ],
        }
    )
    return {
        "text": fallback,
        "attachments": [
            {
                "color": "#0891b2",
                "fallback": fallback,
                "blocks": blocks,
            }
        ],
    }
