"""NEXUS Slack Notifier - Sends Slack notifications for healing events and escalations."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from nexus.services.notifications.notifier_events import (
    send_approval_required,
    send_heal,
    send_incident_escalated,
    send_incident_resolved,
    send_prescale,
    send_report_ready,
    send_slack_http,
)
from nexus.services.notifications.notifier_listener import (
    handle_action_message,
    handle_approval_message,
    handle_incident_escalated_message,
    handle_incident_resolved_message,
    handle_prescale_message,
    handle_report_ready_message,
    listen_notifier_nats,
)
from nexus.services.notifications.slack_templates import build_escalation_payload
from nexus.services.notifications.slack_verifier import (
    SLACK_INTERACTIVE_URL,
    SLACK_REQUEST_TOLERANCE_S,
    SLACK_SIGNING_SECRET,
    approval_buttons_block,
    resolve_slack_webhook,
    verify_slack_request,
)

logger = logging.getLogger(__name__)
_approval_buttons_block = approval_buttons_block

__all__ = [
    "Notifier",
    "send_approval_request_notification",
    "resolve_slack_webhook",
    "verify_slack_request",
    "SLACK_INTERACTIVE_URL",
    "SLACK_SIGNING_SECRET",
    "SLACK_REQUEST_TOLERANCE_S",
    "_approval_buttons_block",
]


class Notifier:
    """Sends Slack notifications for NEXUS healing events."""

    def __init__(self) -> None:
        self._fail_counts: dict[str, int] = {}
        self._nats_task: asyncio.Task | None = None
        self._running: bool = False
        self._nats_client: Any = None

    async def notify_heal(
        self,
        app_name: str,
        runbook_id: str,
        outcome: str,
        description: str,
        target: str | None = None,
    ) -> None:
        await send_heal(self, app_name, runbook_id, outcome, description, target)
        if outcome in ("failed", "rolled_back"):
            self._fail_counts[app_name] = self._fail_counts.get(app_name, 0) + 1
            if self._fail_counts[app_name] >= self._get_page_threshold(app_name):
                await self.notify_escalation(app_name, self._fail_counts[app_name])
                self._fail_counts[app_name] = 0
        else:
            self._fail_counts[app_name] = 0

    async def notify_prescale(
        self,
        app_name: str,
        deployment: str,
        current_rps: float,
        predicted_rps: float,
        horizon_min: int = 10,
        confidence: float = 0.0,
        tables: list[Any] | None = None,
    ) -> None:
        await send_prescale(
            self,
            app_name,
            deployment,
            current_rps,
            predicted_rps,
            horizon_min,
            confidence,
            tables,
        )

    async def notify_escalation(self, app_name: str, failed_attempts: int) -> None:
        webhook = self._get_webhook(app_name)
        if webhook:
            await self._send(
                webhook, build_escalation_payload(app_name, failed_attempts), app_name
            )
            logger.warning(
                f"[Notifier] SRE escalation sent for app='{app_name}' failures={failed_attempts}"
            )

    async def notify_approval_required(
        self,
        app_name: str,
        approval_id: str,
        runbook_id: str,
        target: str,
        healing_level: int,
        confidence: float,
        context: dict | None = None,
    ) -> None:
        await send_approval_required(
            self,
            app_name,
            approval_id,
            runbook_id,
            target,
            healing_level,
            confidence,
            context,
        )

    async def notify_incident_resolved(
        self,
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
        await send_incident_resolved(
            self,
            app_name,
            incident_id,
            target,
            failure_class=failure_class,
            root_cause=root_cause,
            action_taken=action_taken,
            verification_details=verification_details,
            confidence=confidence,
            duration_s=duration_s,
            context=context,
        )

    async def notify_incident_escalated(
        self,
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
        await send_incident_escalated(
            self,
            app_name,
            incident_id,
            target,
            reason=reason,
            failure_class=failure_class,
            root_cause=root_cause,
            context=context,
            confidence=confidence,
            recommended_fix=recommended_fix,
            evidence=evidence,
            proposed_steps=proposed_steps,
            rollback_applied=rollback_applied,
        )

    async def notify_report_ready(
        self,
        app_name: str,
        incident_id: str,
        target: str,
        outcome: str = "resolved",
        summary: str = "",
        report_url: str = "",
    ) -> None:
        await send_report_ready(
            self,
            app_name,
            incident_id,
            target,
            outcome=outcome,
            summary=summary,
            report_url=report_url,
        )

    def _get_webhook(self, app_name: str) -> str | None:
        return resolve_slack_webhook(app_name)

    def _get_page_threshold(self, app_name: str) -> int:
        try:
            from nexus.services.observability.dashboard import _policy_cache

            return int(
                _policy_cache.get(app_name, {})
                .get("notifications", {})
                .get("page_sre_after", 3)
            )
        except Exception:
            return 3

    async def _send(
        self, webhook_url: str, payload: dict[str, Any], app_name: str
    ) -> None:
        await send_slack_http(webhook_url, payload, app_name)

    def start_background(self, nats_client: Any) -> None:
        self._running, self._nats_client = True, nats_client
        self._nats_task = asyncio.create_task(self._listen())
        self._nats_task.add_done_callback(self._on_listen_done)

    def _on_listen_done(self, task: asyncio.Task) -> None:
        if self._running and not task.cancelled() and task.exception():
            try:
                self._nats_task = asyncio.create_task(self._listen())
                self._nats_task.add_done_callback(self._on_listen_done)
            except RuntimeError:
                pass

    def stop(self) -> None:
        self._running = False
        if self._nats_task:
            self._nats_task.cancel()

    async def _listen(self) -> None:
        await listen_notifier_nats(self, self._nats_client)

    async def _on_action_message(self, data: dict) -> None:
        await handle_action_message(self, data)

    async def _on_prescale_message(self, data: dict) -> None:
        await handle_prescale_message(self, data)

    async def _on_approval_message(self, data: dict) -> None:
        await handle_approval_message(self, data)

    async def _on_incident_resolved_message(self, data: dict) -> None:
        await handle_incident_resolved_message(self, data)

    async def _on_incident_escalated_message(self, data: dict) -> None:
        await handle_incident_escalated_message(self, data)

    async def _on_report_ready_message(self, data: dict) -> None:
        await handle_report_ready_message(self, data)


async def send_approval_request_notification(
    incident_id: str, state: dict[str, Any]
) -> None:
    """
    Dispatch a Slack approval notification using the discovered workload identity
    and schema-bound remediation plan when the graph pauses at approval_node.
    """
    target_ns = state.get("namespace") or "unscoped"
    target_resource = state.get("target_resource") or (
        f"{target_ns}/{state.get('resource_kind') or 'Resource'}/{state.get('service_name')}"
        if state.get("service_name")
        else incident_id
    )
    failure_class = str(state.get("failure_class") or "remediation")
    confidence = float(state.get("confidence") or 0.0)
    plan = state.get("remediation_plan") or {}

    notifier = Notifier()
    try:
        await notifier.notify_approval_required(
            app_name=str(target_ns),
            approval_id=incident_id,
            runbook_id=failure_class,
            target=str(target_resource),
            healing_level=3,
            confidence=confidence,
            context={
                "incident_id": incident_id,
                "namespace": state.get("namespace"),
                "resource_kind": state.get("resource_kind"),
                "service_name": state.get("service_name"),
                "target_resource": target_resource,
                "failure_class": failure_class,
                "suspected_cause": state.get("suspected_cause"),
                "rca": state.get("rca_result") or {},
                "plan": plan,
            },
        )
    except Exception as exc:
        logger.debug(
            "[Notifier] Approval notification dispatch skipped for %s: %s",
            incident_id,
            exc,
        )
