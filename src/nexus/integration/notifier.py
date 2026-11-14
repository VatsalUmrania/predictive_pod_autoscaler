"""NEXUS Slack Notifier - Sends Slack notifications for healing events and escalations."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from nexus.integration.notifier_listener import (
    handle_action_message,
    handle_approval_message,
    handle_incident_escalated_message,
    handle_incident_resolved_message,
    handle_prescale_message,
    handle_report_ready_message,
    listen_notifier_nats,
)
from nexus.integration.slack_templates import (
    build_approval_required_payload,
    build_escalation_payload,
    build_heal_payload,
    build_incident_escalated_payload,
    build_incident_resolved_payload,
    build_prescale_payload,
    build_report_ready_payload,
)
from nexus.integration.slack_verifier import (
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
        """Send a healing action notification."""
        webhook = self._get_webhook(app_name)
        if webhook:
            payload = build_heal_payload(
                app_name, runbook_id, outcome, description, target
            )
            await self._send(webhook, payload, app_name)
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
        """Send a pre-scale prediction notification."""
        webhook = self._get_webhook(app_name)
        if webhook:
            payload = build_prescale_payload(
                deployment, current_rps, predicted_rps, horizon_min, confidence, tables
            )
            await self._send(webhook, payload, app_name)

    async def notify_escalation(self, app_name: str, failed_attempts: int) -> None:
        """Send an SRE escalation alert when auto-healing is exhausted."""
        webhook = self._get_webhook(app_name)
        if webhook:
            payload = build_escalation_payload(app_name, failed_attempts)
            await self._send(webhook, payload, app_name)
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
        """Send a notification when an action requires human approval."""
        webhook = self._get_webhook(app_name)
        if webhook:
            payload = build_approval_required_payload(
                app_name,
                approval_id,
                runbook_id,
                target,
                healing_level,
                confidence,
                context,
            )
            await self._send(webhook, payload, app_name)

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
        """Send notification when an incident is successfully resolved."""
        webhook = self._get_webhook(app_name)
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
            await self._send(webhook, payload, app_name)
            logger.info(
                f"[Notifier] Incident resolved notification sent for target='{target}' inc='{incident_id}'"
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
    ) -> None:
        """Send notification when an incident escalates to human operators."""
        webhook = self._get_webhook(app_name)
        if webhook:
            payload = build_incident_escalated_payload(
                target, incident_id, failure_class, reason, root_cause
            )
            await self._send(webhook, payload, app_name)
            logger.warning(
                f"[Notifier] Incident escalation sent for target='{target}' inc='{incident_id}'"
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
        """Post the post-incident report draft to the app's Slack channel."""
        webhook = self._get_webhook(app_name)
        if webhook:
            payload = build_report_ready_payload(target, outcome, summary, report_url)
            await self._send(webhook, payload, app_name)
            logger.info(
                f"[Notifier] Post-incident report posted for target='{target}' inc='{incident_id}'"
            )

    def _get_webhook(self, app_name: str) -> str | None:
        return resolve_slack_webhook(app_name)

    def _get_page_threshold(self, app_name: str) -> int:
        try:
            from nexus.integration.dashboard import _policy_cache

            cfg = _policy_cache.get(app_name, {})
            return int(cfg.get("notifications", {}).get("page_sre_after", 3))
        except Exception:
            return 3

    async def _send(
        self, webhook_url: str, payload: dict[str, Any], app_name: str
    ) -> None:
        """POST a Slack webhook payload asynchronously."""
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
                        f"[Notifier] Slack returned {resp.status_code} for '{app_name}': {resp.text[:100]}"
                    )
        except Exception as exc:
            logger.debug(f"[Notifier] Slack send failed for '{app_name}': {exc}")

    def start_background(self, nats_client: Any) -> None:
        """Subscribe to NATS healing action subjects and auto-notify."""
        self._running = True
        self._nats_client = nats_client
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
        """Stop the background NATS listener."""
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
