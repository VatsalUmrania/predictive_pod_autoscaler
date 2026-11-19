"""Agentic approval Slack Block Kit templates."""

from __future__ import annotations

import json
from typing import Any

from nexus.services.gateway.auth.token_utils import generate_approval_token
from nexus.services.notifications.slack.slack_log_helpers import (
    build_diagnostic_logs_block,
    build_yaml_manifest_block,
)


def build_agentic_approval_payload(
    incident_id: str,
    target_service: str,
    causal_rationale: str,
    proposed_steps: list[dict[str, Any]],
    confidence: float = 1.0,
    platform: str = "kubernetes",
    rollback_plan: list[dict[str, Any]] | None = None,
    approval_id: str | None = None,
) -> dict[str, Any]:
    aid = approval_id or incident_id
    fallback = (
        f"NEXUS Remediation Approval Required: {incident_id} for {target_service}"
    )
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🚨 NEXUS Remediation Approval Required: {target_service}",
                "emoji": True,
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Incident ID:*\n`{incident_id}`"},
                {"type": "mrkdwn", "text": f"*Service:*\n`{target_service}`"},
                {"type": "mrkdwn", "text": f"*Platform:*\n`{platform}`"},
                {"type": "mrkdwn", "text": f"*Confidence:*\n`{confidence:.0%}`"},
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Causal Diagnosis / Hypothesis:*\n{causal_rationale}",
            },
        },
    ]

    from nexus.engine.workflow.state.manifest_formatter import (
        plan_to_yaml_manifest,
    )

    manifest = plan_to_yaml_manifest({"actions": proposed_steps}, target=target_service)
    blocks.append(build_yaml_manifest_block(manifest))
    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"🛡️ *NEXUS Agentic Governance Engine* • Incident: `{incident_id}` • Service: `{target_service}`",
                }
            ],
        }
    )
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve", "emoji": True},
                    "style": "primary",
                    "action_id": "nexus_approve",
                    "value": aid,
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Edit Arguments",
                        "emoji": True,
                    },
                    "action_id": "nexus_edit",
                    "value": aid,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Reject", "emoji": True},
                    "style": "danger",
                    "action_id": "nexus_reject",
                    "value": aid,
                    "confirm": {
                        "title": {
                            "type": "plain_text",
                            "text": "Reject Remediation?",
                            "emoji": True,
                        },
                        "text": {
                            "type": "mrkdwn",
                            "text": "Are you sure you want to reject this automated remediation plan? It will escalate to on-call SREs.",
                        },
                        "confirm": {
                            "type": "plain_text",
                            "text": "Reject & Escalate",
                            "emoji": True,
                        },
                        "deny": {"type": "plain_text", "text": "Cancel", "emoji": True},
                        "style": "danger",
                    },
                },
            ],
        }
    )

    return {
        "text": fallback,
        "attachments": [
            {
                "color": "#ff9900",
                "fallback": fallback,
                "blocks": blocks,
            }
        ],
    }


def build_slack_approval_blocks(
    incident_id: str,
    rca_report: dict[str, Any],
    remediation_plan: dict[str, Any],
    signing_secret: str = "",
) -> list[dict[str, Any]]:
    """Build the official 1-Click Slack approval Block Kit card with HMAC buttons."""
    approve_tok = generate_approval_token(incident_id, "approve", signing_secret)
    reject_tok = generate_approval_token(incident_id, "reject", signing_secret)

    evidence_lines = (
        "\n".join(
            f"• `{ev.get('tool_name', 'tool')}`: {ev.get('key_finding', '')}"
            for ev in rca_report.get("evidence_chain", [])[:4]
        )
        or "• _Verified by telemetry inspection_"
    )

    from nexus.engine.workflow.state.manifest_formatter import (
        extract_diagnostic_logs,
        plan_to_yaml_manifest,
    )

    urn = str(rca_report.get("affected_resource_urn", "unknown"))
    conf = float(rca_report.get("confidence", 0.0))

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🚨 NEXUS RCA & 1-Click Fix Plan [{incident_id}]",
                "emoji": True,
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Resource:*\n`{urn}`"},
                {"type": "mrkdwn", "text": f"*Confidence:*\n`{conf:.0%}`"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Root Cause Summary:*\n{rca_report.get('root_cause_summary', 'Root cause identified')}\n\n*Verified Tool Evidence:*\n{evidence_lines}",
            },
        },
    ]

    diag_logs = extract_diagnostic_logs(rca_report)
    if diag_logs:
        blocks.append({"type": "divider"})
        blocks.append(build_diagnostic_logs_block(diag_logs))

    manifest_yaml = plan_to_yaml_manifest(remediation_plan, target=urn)
    blocks.append({"type": "divider"})
    blocks.append(build_yaml_manifest_block(manifest_yaml))

    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "actions",
            "block_id": f"approval_actions_{incident_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "✅ Approve & Auto-Fix Now",
                        "emoji": True,
                    },
                    "style": "primary",
                    "action_id": "nexus_approve_fix",
                    "value": json.dumps(
                        {
                            "incident_id": incident_id,
                            "decision": "approve",
                            "token": approve_tok,
                        }
                    ),
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "❌ Reject & Escalate",
                        "emoji": True,
                    },
                    "style": "danger",
                    "action_id": "nexus_reject_fix",
                    "value": json.dumps(
                        {
                            "incident_id": incident_id,
                            "decision": "reject",
                            "token": reject_tok,
                        }
                    ),
                    "confirm": {
                        "title": {
                            "type": "plain_text",
                            "text": "Reject & Escalate?",
                            "emoji": True,
                        },
                        "text": {
                            "type": "mrkdwn",
                            "text": "Rejecting will escalate this incident directly to the on-call SRE team without auto-remediation.",
                        },
                        "confirm": {
                            "type": "plain_text",
                            "text": "Reject & Escalate",
                            "emoji": True,
                        },
                        "deny": {"type": "plain_text", "text": "Cancel", "emoji": True},
                        "style": "danger",
                    },
                },
            ],
        }
    )
    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"🛡️ *NEXUS Autonomous Governance* • Cryptographically signed 1-Click action • Incident: `{incident_id}`",
                }
            ],
        }
    )
    return blocks
