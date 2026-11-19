"""
Slack notification event senders for NEXUS Notifier.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from nexus.services.notifications.slack.slack_templates import (
    build_approval_required_payload,
    build_heal_payload,
    build_incident_escalated_payload,
    build_incident_resolved_payload,
    build_prescale_payload,
    build_report_ready_payload,
)

logger = logging.getLogger(__name__)


async def send_heal(
    notifier: Any,
    app_name: str,
    runbook_id: str,
    outcome: str,
    description: str,
    target: str | None = None,
) -> None:
    webhook = notifier._get_webhook(app_name)
    if webhook:
        await notifier._send(
            webhook,
            build_heal_payload(app_name, runbook_id, outcome, description, target),
            app_name,
        )


async def send_prescale(
    notifier: Any,
    app_name: str,
    deployment: str,
    current_rps: float,
    predicted_rps: float,
    horizon_min: int = 10,
    confidence: float = 0.0,
    tables: list[Any] | None = None,
) -> None:
    webhook = notifier._get_webhook(app_name)
    if webhook:
        await notifier._send(
            webhook,
            build_prescale_payload(
                deployment, current_rps, predicted_rps, horizon_min, confidence, tables
            ),
            app_name,
        )


async def send_approval_required(
    notifier: Any,
    app_name: str,
    approval_id: str,
    runbook_id: str,
    target: str,
    healing_level: int,
    confidence: float,
    context: dict | None = None,
) -> None:
    webhook = notifier._get_webhook(app_name)
    if webhook:
        await notifier._send(
            webhook,
            build_approval_required_payload(
                app_name,
                approval_id,
                runbook_id,
                target,
                healing_level,
                confidence,
                context,
            ),
            app_name,
        )


async def send_slack_http(
    webhook_url: str, payload: dict[str, Any], app_name: str
) -> None:
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                webhook_url,
                content=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code != 200:
                logger.warning(
                    f"[Notifier] Slack returned {resp.status_code} for '{app_name}'"
                )
    except Exception as exc:
        logger.debug(f"[Notifier] Slack send failed for '{app_name}': {exc}")


async def send_incident_resolved(
    notifier: Any,
    app_name: str,
    incident_id: str,
    target: str,
    failure_class: str = "Unknown",
    root_cause: str = "Unknown",
    action_taken: str = "Automated remediation",
    verification_details: str = "Target verified healthy and SLO restored",
    confidence: float = 1.0,
    duration_s: float | None = None,
    context: dict | None = None,
) -> None:
    webhook = notifier._get_webhook(app_name)
    if webhook:
        payload = build_incident_resolved_payload(
            target,
            incident_id,
            failure_class,
            confidence,
            duration_s,
            root_cause,
            action_taken,
            verification_details,
        )
        await notifier._send(webhook, payload, app_name)
        logger.info(
            f"[Notifier] Incident resolved notification sent for target='{target}' inc='{incident_id}'"
        )


async def send_incident_escalated(
    notifier: Any,
    app_name: str,
    incident_id: str,
    target: str,
    reason: str = "Remediation failed or maximum retries exhausted",
    failure_class: str = "Unknown",
    root_cause: str = "Unknown",
    context: dict | None = None,
    *,
    confidence: float | None = None,
    recommended_fix: str | None = None,
    evidence: list[str] | None = None,
    proposed_steps: list[dict[str, Any]] | None = None,
    rollback_applied: bool = False,
) -> None:
    webhook = notifier._get_webhook(app_name)
    if webhook:
        ctx = context or {}
        rca = ctx.get("rca") or ctx.get("rca_result") or {}
        plan = ctx.get("plan") or ctx.get("remediation_plan") or {}
        resolved_conf = (
            confidence
            if confidence is not None
            else (float(rca.get("confidence") or ctx.get("confidence") or 0.0) or None)
        )
        resolved_fix = (
            recommended_fix
            or ctx.get("recommended_strategy")
            or rca.get("recommended_strategy")
            or plan.get("rationale")
        )
        resolved_evidence = (
            evidence
            if evidence is not None
            else (ctx.get("evidence") or rca.get("evidence") or None)
        )
        resolved_steps = (
            proposed_steps
            if proposed_steps is not None
            else (plan.get("steps") or None)
        )
        resolved_rollback = bool(
            rollback_applied
            or ctx.get("rollback_applied")
            or ctx.get("rollback_results")
        )
        payload = build_incident_escalated_payload(
            target,
            incident_id,
            failure_class,
            reason,
            root_cause,
            confidence=resolved_conf,
            recommended_fix=resolved_fix,
            evidence=resolved_evidence,
            proposed_steps=resolved_steps,
            rollback_applied=resolved_rollback,
        )
        await notifier._send(webhook, payload, app_name)
        logger.warning(
            f"[Notifier] Incident escalation sent for target='{target}' inc='{incident_id}'"
        )


async def send_report_ready(
    notifier: Any,
    app_name: str,
    incident_id: str,
    target: str,
    outcome: str = "resolved",
    summary: str = "",
    report_url: str = "",
) -> None:
    webhook = notifier._get_webhook(app_name)
    if webhook:
        payload = build_report_ready_payload(target, outcome, summary, report_url)
        await notifier._send(webhook, payload, app_name)
        logger.info(
            f"[Notifier] Post-incident report posted for target='{target}' inc='{incident_id}'"
        )
