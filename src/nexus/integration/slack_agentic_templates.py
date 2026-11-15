"""Agentic approval Slack Block Kit templates."""

from __future__ import annotations

from typing import Any


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
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🚨 NEXUS Remediation Approval Required: {target_service}",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Incident ID:*\n`{incident_id}`"},
                {"type": "mrkdwn", "text": f"*Service:*\n`{target_service}`"},
                {"type": "mrkdwn", "text": f"*Platform:*\n`{platform}`"},
                {"type": "mrkdwn", "text": f"*Confidence:*\n`{confidence:.0%}`"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Causal Diagnosis / Hypothesis:*\n{causal_rationale}",
            },
        },
    ]

    steps_lines = []
    for idx, step in enumerate(proposed_steps):
        tname = step.get("tool_name", "unknown")
        bradius = step.get("blast_radius", "service")
        destr_tag = " ⚠️ *DESTRUCTIVE*" if step.get("is_destructive") else ""
        steps_lines.append(
            f"*{idx + 1}. `{tname}`* (Blast Radius: `{bradius}`{destr_tag})\nParameters: `{step.get('parameters', {})}`"
        )
    if steps_lines:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Proposed Tool Steps:*\n" + "\n".join(steps_lines),
                },
            }
        )

    rollback_lines = []
    if rollback_plan:
        for idx, rb in enumerate(rollback_plan):
            rollback_lines.append(
                f"*{idx + 1}. `{rb.get('tool_name', 'unknown')}`*\nParameters: `{rb.get('parameters', {})}`"
            )
    else:
        for idx, step in enumerate(proposed_steps):
            if step.get("rollback_tool"):
                rollback_lines.append(
                    f"*{idx + 1}. `{step['rollback_tool']}`*\nParameters: `{step.get('rollback_parameters', {})}`"
                )

    if rollback_lines:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Rollback Plan:*\n" + "\n".join(rollback_lines),
                },
            }
        )
    else:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Rollback Plan:*\n_None (unbacked rollback permitted or self-contained)_",
                },
            }
        )

    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": "nexus_approve",
                    "value": aid,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Edit Arguments"},
                    "action_id": "nexus_edit",
                    "value": aid,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "style": "danger",
                    "action_id": "nexus_reject",
                    "value": aid,
                },
            ],
        }
    )

    return {
        "attachments": [
            {
                "color": "#ff9900",
                "fallback": f"NEXUS Remediation Approval Required: {incident_id} for {target_service}",
                "blocks": blocks,
            }
        ]
    }
