"""Action and approval Slack Block Kit templates for Notifier."""

from __future__ import annotations

from typing import Any

from nexus.integration.slack_agentic_templates import build_agentic_approval_payload
from nexus.integration.slack_verifier import approval_buttons_block

__all__ = [
    "build_heal_payload",
    "build_prescale_payload",
    "build_escalation_payload",
    "build_approval_required_payload",
    "build_agentic_approval_payload",
]


def build_heal_payload(
    app_name: str,
    runbook_id: str,
    outcome: str,
    description: str,
    target: str | None = None,
) -> dict[str, Any]:
    color = "#83ef48" if outcome == "success" else "#ff0000"
    return {
        "attachments": [
            {
                "color": color,
                "fallback": f"NEXUS healed {app_name}: {description}",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*NEXUS Healing Action — `{app_name}`*",
                        },
                    },
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": f"*Action:*\n{description}"},
                            {"type": "mrkdwn", "text": f"*Outcome:*\n`{outcome}`"},
                            {"type": "mrkdwn", "text": f"*Runbook:*\n`{runbook_id}`"},
                            {
                                "type": "mrkdwn",
                                "text": f"*Target:*\n`{target or 'unknown'}`",
                            },
                        ],
                    },
                ],
            }
        ]
    }


def build_prescale_payload(
    deployment: str,
    current_rps: float,
    predicted_rps: float,
    horizon_min: int = 10,
    confidence: float = 0.0,
    tables: list[Any] | None = None,
) -> dict[str, Any]:
    tables = tables or []
    mult = predicted_rps / current_rps if current_rps > 0 else 1.0
    table_clause = (
        f" — DB spike on {', '.join(f'`{t}`' for t in tables[:3])}" if tables else ""
    )
    return {
        "attachments": [
            {
                "color": "#ECB22E",
                "fallback": f"NEXUS pre-scaling {deployment}",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*NEXUS Pre-Scale — `{deployment}`*",
                        },
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"Predicted *{mult:.1f}x* traffic in ~{horizon_min} min ({current_rps:.0f} → {predicted_rps:.0f} RPS){table_clause}.\nConfidence: *{confidence:.0%}*",
                        },
                    },
                ],
            }
        ]
    }


def build_escalation_payload(app_name: str, failed_attempts: int) -> dict[str, Any]:
    return {
        "attachments": [
            {
                "color": "#e01e5a",
                "fallback": f"NEXUS: {app_name} requires human review",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*SRE Escalation — `{app_name}` requires human review*\n\nNEXUS has made *{failed_attempts}* failed healing attempts and has stopped autonomous action.\nRun `nexus audit --n 10` to see full history.\nApprove or reject at: `nexus approvals`",
                        },
                    },
                ],
            }
        ]
    }


def build_approval_required_payload(
    app_name: str,
    approval_id: str,
    runbook_id: str,
    target: str,
    healing_level: int,
    confidence: float,
    context: dict | None = None,
) -> dict[str, Any]:
    rca = (context or {}).get("rca", {})
    event = (context or {}).get("event", {})
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*NEXUS L{healing_level} Action Requires Approval*",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Runbook:*\n`{runbook_id}`"},
                {"type": "mrkdwn", "text": f"*Target:*\n`{target}`"},
                {"type": "mrkdwn", "text": f"*Confidence:*\n`{confidence:.2f}`"},
                {"type": "mrkdwn", "text": f"*Approval ID:*\n`{approval_id}`"},
            ],
        },
    ]
    if rca:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Root Cause Analysis*\n*Class:* {rca.get('failure_class', 'Unknown')}\n*Root Cause:* {rca.get('root_cause', 'Unknown')[:300]}",
                },
            }
        )
    if event:
        blocks.append(
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Signal:*\n`{event.get('signal_type', 'unknown')}`",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Severity:*\n`{event.get('severity', 'unknown')}`",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Namespace:*\n`{event.get('namespace', 'unknown')}`",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*RCA Source:*\n`{(context or {}).get('rca_source', 'unknown')}`",
                    },
                ],
            }
        )
    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"To approve this action, run:\n`nexus approve {approval_id}`",
            },
        }
    )
    blocks.append(approval_buttons_block(approval_id))
    return {
        "attachments": [
            {
                "color": "#ff9900",
                "fallback": f"NEXUS L{healing_level} Action Requires Approval: {runbook_id} for {target}",
                "blocks": blocks,
            }
        ]
    }
