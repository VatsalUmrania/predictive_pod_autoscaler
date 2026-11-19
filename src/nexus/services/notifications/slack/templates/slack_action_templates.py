"""Action and approval Slack Block Kit templates for Notifier."""

from __future__ import annotations

from typing import Any

from nexus.services.notifications.slack.slack_log_helpers import (
    build_diagnostic_logs_block,
    build_yaml_manifest_block,
)
from nexus.services.notifications.slack.slack_verifier import approval_buttons_block
from nexus.services.notifications.slack.templates.slack_agentic_templates import (
    build_agentic_approval_payload,
    build_slack_approval_blocks,
)

__all__ = [
    "build_heal_payload",
    "build_prescale_payload",
    "build_escalation_payload",
    "build_approval_required_payload",
    "build_agentic_approval_payload",
    "build_slack_approval_blocks",
]


def build_heal_payload(
    app_name: str,
    runbook_id: str,
    outcome: str,
    description: str,
    target: str | None = None,
) -> dict[str, Any]:
    color = "#83ef48" if outcome == "success" else "#ff0000"
    status_badge = "🟢 Success" if outcome == "success" else "🔴 Failed"
    fallback = f"NEXUS healed {app_name}: {description}"
    return {
        "text": fallback,
        "attachments": [
            {
                "color": color,
                "fallback": fallback,
                "blocks": [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": f"NEXUS Healing Action — {app_name}",
                            "emoji": True,
                        },
                    },
                    {"type": "divider"},
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": f"*Action:*\n{description}"},
                            {"type": "mrkdwn", "text": f"*Outcome:*\n`{outcome}`"},
                            {
                                "type": "mrkdwn",
                                "text": f"*Target:*\n`{target or 'unknown'}`",
                            },
                            {
                                "type": "mrkdwn",
                                "text": f"*Status:*\n`{status_badge}`",
                            },
                        ],
                    },
                    {"type": "divider"},
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": f"🤖 *NEXUS Autonomous Engine* | Runbook: `{runbook_id}`",
                            }
                        ],
                    },
                ],
            }
        ],
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
    fallback = f"NEXUS pre-scaling {deployment}"
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"📈 NEXUS Pre-Scale — {deployment}",
                "emoji": True,
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Deployment:*\n`{deployment}`"},
                {
                    "type": "mrkdwn",
                    "text": f"*Traffic Spike:*\n*{mult:.1f}x* ({current_rps:.0f} → {predicted_rps:.0f} RPS)",
                },
                {"type": "mrkdwn", "text": f"*Horizon:*\n~{horizon_min} min"},
                {"type": "mrkdwn", "text": f"*Confidence:*\n*{confidence:.0%}*"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"Predicted *{mult:.1f}x* traffic in ~{horizon_min} min ({current_rps:.0f} → {predicted_rps:.0f} RPS){table_clause}.\nConfidence: *{confidence:.0%}*",
            },
        },
        {"type": "divider"},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "🤖 *NEXUS AI Predictor* • Proactive scaling initiated",
                }
            ],
        },
    ]
    return {
        "text": fallback,
        "attachments": [
            {
                "color": "#ECB22E",
                "fallback": fallback,
                "blocks": blocks,
            }
        ],
    }


def build_escalation_payload(app_name: str, failed_attempts: int) -> dict[str, Any]:
    fallback = f"NEXUS: {app_name} requires human review"
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🚨 SRE Escalation — {app_name}",
                "emoji": True,
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Application:*\n`{app_name}`"},
                {"type": "mrkdwn", "text": "*Status:*\n`AUTONOMOUS HALT`"},
                {
                    "type": "mrkdwn",
                    "text": f"*Failed Attempts:*\n*{failed_attempts}* attempts",
                },
                {"type": "mrkdwn", "text": "*Severity:*\n`P1 / HIGH`"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*SRE Escalation — `{app_name}` requires human review*\n\nNEXUS has made *{failed_attempts}* failed healing attempts and has stopped autonomous action.\nRun `nexus audit --n 10` to see full history.\nApprove or reject at: `nexus approvals`",
            },
        },
        {"type": "divider"},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "⚠️ *Requires on-call SRE intervention* | Escalate via on-call channel",
                }
            ],
        },
    ]
    return {
        "text": fallback,
        "attachments": [
            {
                "color": "#e01e5a",
                "fallback": fallback,
                "blocks": blocks,
            }
        ],
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
    ctx = context or {}
    rca = ctx.get("rca") or ctx.get("rca_result") or {}
    event = ctx.get("event") or {}
    rca_target_raw = rca.get("target")
    rca_target: dict[str, Any] = (
        dict(rca_target_raw) if isinstance(rca_target_raw, dict) else {}
    )

    resolved_target = (
        target
        or ctx.get("target_resource")
        or (
            f"{ctx.get('namespace') or rca_target.get('namespace_or_region', 'unknown')}/"
            f"{ctx.get('resource_kind') or rca_target.get('resource_kind', 'Deployment')}/"
            f"{ctx.get('service_name') or rca_target.get('resource_name', 'unknown')}"
            if (ctx.get("service_name") or rca_target.get("resource_name"))
            else "unknown"
        )
    )
    failure_type = (
        rca.get("failure_type")
        or ctx.get("failure_class")
        or rca.get("failure_class")
        or runbook_id
        or "unclassified_anomaly"
    )
    root_cause = (
        rca.get("root_cause_summary")
        or ctx.get("suspected_cause")
        or rca.get("root_cause")
        or "No root cause summary available."
    )
    recommended_strategy = (
        rca.get("recommended_strategy") or ctx.get("suggested_fix") or ""
    )

    fallback = f"NEXUS L{healing_level} Action Requires Approval: {failure_type} for {resolved_target}"
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🛡️ NEXUS L{healing_level} Action Requires Approval",
                "emoji": True,
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Target:*\n`{resolved_target}`"},
                {"type": "mrkdwn", "text": f"*Confidence:*\n`{confidence:.2f}`"},
                {"type": "mrkdwn", "text": f"*Approval ID:*\n`{approval_id}`"},
                {"type": "mrkdwn", "text": f"*Level:*\n`L{healing_level}`"},
            ],
        },
    ]
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
                        "text": f"*RCA Source:*\n`{ctx.get('rca_source', 'autonomous')}`",
                    },
                ],
            }
        )
    if rca or ctx.get("suspected_cause") or ctx.get("failure_class"):
        rca_lines = [
            "*Root Cause Analysis*",
            f"*Class:* `{failure_type}`",
            f"*Root Cause:* {str(root_cause)[:500]}",
        ]
        if recommended_strategy:
            rca_lines.append(f"*Recommended Fix:* {str(recommended_strategy)[:400]}")
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "\n".join(rca_lines),
                },
            }
        )

    diag_logs = ctx.get("diagnostic_logs")
    if not diag_logs:
        from nexus.engine.workflow.state.manifest_formatter import (
            extract_diagnostic_logs,
        )

        diag_logs = extract_diagnostic_logs(ctx)
    if diag_logs:
        blocks.append({"type": "divider"})
        blocks.append(build_diagnostic_logs_block(diag_logs))

    rem_yaml = ctx.get("remediation_yaml")
    if not rem_yaml:
        from nexus.engine.workflow.state.manifest_formatter import (
            plan_to_yaml_manifest,
        )

        rem_yaml = plan_to_yaml_manifest(ctx.get("plan"), resolved_target)
    if rem_yaml:
        blocks.append({"type": "divider"})
        blocks.append(build_yaml_manifest_block(rem_yaml))

    blocks.append({"type": "divider"})
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
    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"🤖 *NEXUS Governance Engine* • Target: `{resolved_target}` • Approval ID: `{approval_id}`",
                }
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
