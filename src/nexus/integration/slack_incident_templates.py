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

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"✅ *NEXUS Incident Resolved — `{target}`*{dur_str}",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Incident ID:*\n`{incident_id}`"},
                {"type": "mrkdwn", "text": f"*Target:*\n`{target}`"},
                {"type": "mrkdwn", "text": f"*Failure Class:*\n`{failure_class}`"},
                {"type": "mrkdwn", "text": f"*Confidence:*\n`{conf_str}`"},
            ],
        },
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
    ]

    return {
        "attachments": [
            {
                "color": "#059669",
                "fallback": f"✅ NEXUS Incident Resolved for {target}: {failure_class}",
                "blocks": blocks,
            }
        ]
    }


def build_incident_escalated_payload(
    target: str,
    incident_id: str,
    failure_class: str,
    reason: str,
    root_cause: str,
) -> dict[str, Any]:
    """Build Slack Block Kit payload for escalated incident."""
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"🚨 *NEXUS Incident Escalation — `{target}` requires human intervention*",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Incident ID:*\n`{incident_id}`"},
                {"type": "mrkdwn", "text": f"*Target:*\n`{target}`"},
                {"type": "mrkdwn", "text": f"*Failure Class:*\n`{failure_class}`"},
                {"type": "mrkdwn", "text": "*Status:*\n`ESCALATED`"},
            ],
        },
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
    return {
        "attachments": [
            {
                "color": "#dc2626",
                "fallback": f"🚨 NEXUS Incident Escalated for {target}: {reason}",
                "blocks": blocks,
            }
        ]
    }


def build_report_ready_payload(
    target: str,
    outcome: str,
    summary: str,
    report_url: str,
) -> dict[str, Any]:
    """Build Slack Block Kit payload for post-incident report."""
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"📄 *NEXUS Post-Incident Report — `{target}`*",
            },
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
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"<{report_url}|Open full report>"},
            }
        )
    return {
        "attachments": [
            {
                "color": "#0891b2",
                "fallback": f"NEXUS post-incident report ready for {target} ({outcome})",
                "blocks": blocks,
            }
        ]
    }
