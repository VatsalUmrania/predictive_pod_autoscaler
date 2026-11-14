"""Action and approval Slack Block Kit templates for Notifier."""

from __future__ import annotations

from typing import Any

from nexus.integration.slack_verifier import approval_buttons_block


def build_heal_payload(
    app_name: str,
    runbook_id: str,
    outcome: str,
    description: str,
    target: str | None = None,
) -> dict[str, Any]:
    """Build Slack Block Kit payload for a healing action."""
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
    """Build Slack Block Kit payload for a pre-scale event."""
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
    """Build Slack Block Kit payload for SRE escalation."""
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
                            "text": f"*SRE Escalation — `{app_name}` requires human review*\n\nNEXUS has made *{failed_attempts}* failed healing attempts and has stopped autonomous action.\nRun `nexus audit --n 10` to see the full incident history.\nApprove or reject actions at: `nexus approvals`",
                        },
                    }
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
    """Build Slack Block Kit payload for approval requests."""
    rca = (context or {}).get("rca", {})
    event = (context or {}).get("event", {})
    rca_source = (context or {}).get("rca_source", "unknown")

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
        root_cause = rca.get("root_cause", "Unknown")
        failure_class = rca.get("failure_class", "Unknown")
        reasoning = rca.get("reasoning", "")
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Root Cause Analysis*\n*Class:* {failure_class}\n*Root Cause:* {root_cause[:300]}{'...' if len(root_cause) > 300 else ''}",
                },
            }
        )
        if reasoning:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Reasoning:*\n{reasoning[:500]}{'...' if len(reasoning) > 500 else ''}",
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
                    {"type": "mrkdwn", "text": f"*RCA Source:*\n`{rca_source}`"},
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
    """Format rich Slack Block Kit approval card for autonomous remediation plans."""
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

    # Proposed Tool Steps
    steps_lines: list[str] = []
    for idx, step in enumerate(proposed_steps):
        tname = step.get("tool_name", "unknown")
        bradius = step.get("blast_radius", "service")
        params = step.get("parameters", {})
        destr = step.get("is_destructive", False)
        destr_tag = " ⚠️ *DESTRUCTIVE*" if destr else ""
        steps_lines.append(
            f"*{idx + 1}. `{tname}`* (Blast Radius: `{bradius}`{destr_tag})\nParameters: `{params}`"
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

    # Rollback Plan
    rollback_lines: list[str] = []
    if rollback_plan:
        for idx, rb in enumerate(rollback_plan):
            tname = rb.get("tool_name", "unknown")
            params = rb.get("parameters", {})
            rollback_lines.append(f"*{idx + 1}. `{tname}`*\nParameters: `{params}`")
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

    # Interactive Buttons
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
