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
    *,
    confidence: float | None = None,
    recommended_fix: str | None = None,
    evidence: list[str] | None = None,
    proposed_steps: list[dict[str, Any]] | None = None,
    rollback_applied: bool = False,
) -> dict[str, Any]:
    """Build Slack Block Kit payload for escalated incident with RCA & proposed fix."""
    header_text = f"🚨 NEXUS Incident Escalation — {target}"
    if len(header_text) > 150:
        header_text = header_text[:147] + "..."
    fallback = f"🚨 NEXUS Incident Escalated for {target}: {failure_class} — {reason[:160]}"

    fields: list[dict[str, str]] = [
        {"type": "mrkdwn", "text": f"*Incident ID:*\n`{incident_id}`"},
        {"type": "mrkdwn", "text": f"*Target:*\n`{target}`"},
        {"type": "mrkdwn", "text": f"*Failure Class:*\n`{failure_class}`"},
        {"type": "mrkdwn", "text": "*Status:*\n`ESCALATED`"},
    ]
    if confidence is not None and confidence > 0.0:
        conf_str = (
            f"{confidence * 100:.0f}%" if confidence <= 1.0 else f"{confidence:.0f}%"
        )
        fields.append({"type": "mrkdwn", "text": f"*RCA Confidence:*\n`{conf_str}`"})
    fields.append(
        {"type": "mrkdwn", "text": f"*Rollback Applied:*\n`{rollback_applied}`"}
    )

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": header_text,
                "emoji": True,
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": fields,
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Escalation Reason:*\n{reason[:500]}{'...' if len(reason) > 500 else ''}",
            },
        },
    ]

    if root_cause and root_cause != "Unknown":
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Root Cause Analysis (RCA):*\n{root_cause[:600]}{'...' if len(root_cause) > 600 else ''}",
                },
            }
        )

    fix_lines: list[str] = []
    if recommended_fix and recommended_fix.strip():
        fix_lines.append(recommended_fix.strip()[:600])
    if proposed_steps:
        step_summaries = []
        for idx, step in enumerate(proposed_steps[:4], start=1):
            tool_name = step.get("tool_name") or step.get("action") or "step"
            desc = step.get("description") or str(step.get("tool_args") or "")
            step_summaries.append(f"{idx}. `{tool_name}` — {desc[:180]}")
        if step_summaries:
            fix_lines.append("\n".join(step_summaries))

    if fix_lines:
        fix_text = "\n".join(fix_lines)
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Proposed Fix / Recommended Strategy:*\n{fix_text[:1000]}{'...' if len(fix_text) > 1000 else ''}",
                },
            }
        )

    if evidence:
        ev_items = [f"• {str(item)[:200]}" for item in evidence[:4] if str(item).strip()]
        if ev_items:
            ev_text = "\n".join(ev_items)
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Observed Evidence:*\n{ev_text[:900]}",
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
