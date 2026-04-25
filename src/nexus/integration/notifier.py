"""
NEXUS Slack Notifier
=====================
Sends Slack webhook notifications for healing events, pre-scale decisions,
and SRE escalations. Reads webhook URL + paging threshold from each app's
selfheal.yaml (via the policy cache in dashboard.py).

Notification types:
    🔧 Healing action taken  — after every RunbookExecutor outcome
    📈 Pre-scale initiated   — when Prescaler makes a decision (advisory/autonomous)
    🚨 SRE escalation        — when healing fails ≥ page_sre_after times for an app

Usage (from Orchestrator or FeedbackLoop):
    from nexus.integration.notifier import Notifier
    notifier = Notifier()
    await notifier.notify_heal(app_name, runbook_id, outcome, description)
    await notifier.notify_prescale(app_name, deployment, current_rps, predicted_rps)
    await notifier.notify_escalation(app_name, failed_attempts)

NATS integration (background mode):
    notifier.start_background(nats_client)   # subscribes to nexus.actions.*
    notifier.stop()
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class Notifier:
    """
    Sends Slack notifications for NEXUS healing events.

    Webhook URLs come from the app's selfheal.yaml policy cache.
    If an app has no slack_webhook configured, notifications are silently skipped.
    """

    def __init__(self) -> None:
        self._fail_counts:  Dict[str, int] = {}   # app_name → consecutive failures
        self._nats_task:    Optional[asyncio.Task] = None
        self._running:      bool = False

    # ── Public notification API ───────────────────────────────────────────────

    async def notify_heal(
        self,
        app_name:    str,
        runbook_id:  str,
        outcome:     str,
        description: str,
        target:      Optional[str] = None,
    ) -> None:
        """Send a healing action notification."""
        webhook = self._get_webhook(app_name)
        if not webhook:
            return

        icon   = "✅" if outcome == "success" else ("❌" if outcome == "failed" else "↩️")
        color  = "#36a64f" if outcome == "success" else "#e01e5a"

        payload = {
            "attachments": [{
                "color":    color,
                "fallback": f"NEXUS healed {app_name}: {description}",
                "blocks": [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"*{icon} NEXUS Healing Action — `{app_name}`*"},
                    },
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": f"*Action:*\n{description}"},
                            {"type": "mrkdwn", "text": f"*Outcome:*\n`{outcome}`"},
                            {"type": "mrkdwn", "text": f"*Runbook:*\n`{runbook_id}`"},
                            {"type": "mrkdwn", "text": f"*Target:*\n`{target or 'unknown'}`"},
                        ],
                    },
                ],
            }]
        }

        await self._send(webhook, payload, app_name)

        # Track failures for escalation
        if outcome in ("failed", "rolled_back"):
            self._fail_counts[app_name] = self._fail_counts.get(app_name, 0) + 1
            threshold = self._get_page_threshold(app_name)
            if self._fail_counts[app_name] >= threshold:
                await self.notify_escalation(app_name, self._fail_counts[app_name])
                self._fail_counts[app_name] = 0   # reset after escalation
        else:
            self._fail_counts[app_name] = 0       # reset on success

    async def notify_prescale(
        self,
        app_name:      str,
        deployment:    str,
        current_rps:   float,
        predicted_rps: float,
        horizon_min:   int      = 10,
        confidence:    float    = 0.0,
        tables:        list     = None,
    ) -> None:
        """Send a pre-scale prediction notification."""
        webhook = self._get_webhook(app_name)
        if not webhook:
            return

        tables = tables or []
        mult   = predicted_rps / current_rps if current_rps > 0 else 1.0
        table_clause = (
            f" — DB spike on {', '.join(f'`{t}`' for t in tables[:3])}"
            if tables else ""
        )

        payload = {
            "attachments": [{
                "color": "#ECB22E",
                "fallback": f"NEXUS pre-scaling {deployment}",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*📈 NEXUS Pre-Scale — `{deployment}`*",
                        },
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f"Predicted *{mult:.1f}x* traffic in ~{horizon_min} min "
                                f"({current_rps:.0f} → {predicted_rps:.0f} RPS)"
                                f"{table_clause}.\n"
                                f"Confidence: *{confidence:.0%}*"
                            ),
                        },
                    },
                ],
            }]
        }

        await self._send(webhook, payload, app_name)

    async def notify_escalation(
        self,
        app_name:        str,
        failed_attempts: int,
    ) -> None:
        """Send an SRE escalation alert when auto-healing is exhausted."""
        webhook = self._get_webhook(app_name)
        if not webhook:
            return

        payload = {
            "attachments": [{
                "color": "#e01e5a",
                "fallback": f"NEXUS: {app_name} requires human review",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f"*🚨 SRE Escalation — `{app_name}` requires human review*\n\n"
                                f"NEXUS has made *{failed_attempts}* failed healing attempts "
                                f"and has stopped autonomous action.\n"
                                f"Run `nexus audit --n 10` to see the full incident history.\n"
                                f"Approve or reject actions at: `nexus approvals`"
                            ),
                        },
                    },
                ],
            }]
        }

        await self._send(webhook, payload, app_name)
        logger.warning(f"[Notifier] SRE escalation sent for app='{app_name}' failures={failed_attempts}")

    # ── Policy helpers ────────────────────────────────────────────────────────

    def _get_webhook(self, app_name: str) -> Optional[str]:
        """Return the Slack webhook URL for an app, or None if not configured."""
        try:
            from nexus.integration.dashboard import _policy_cache
            cfg = _policy_cache.get(app_name, {})
            notifications = cfg.get("notifications", {})
            return notifications.get("slack_webhook") or None
        except Exception:
            return None

    def _get_page_threshold(self, app_name: str) -> int:
        """Return the page_sre_after threshold for an app (default 3)."""
        try:
            from nexus.integration.dashboard import _policy_cache
            cfg = _policy_cache.get(app_name, {})
            return cfg.get("notifications", {}).get("page_sre_after", 3)
        except Exception:
            return 3

    # ── HTTP send ─────────────────────────────────────────────────────────────

    async def _send(
        self,
        webhook_url: str,
        payload:     Dict[str, Any],
        app_name:    str,
    ) -> None:
        """POST a Slack webhook payload asynchronously."""
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    webhook_url,
                    content     = json.dumps(payload).encode(),
                    headers     = {"Content-Type": "application/json"},
                )
                if resp.status_code != 200:
                    logger.warning(
                        f"[Notifier] Slack webhook returned {resp.status_code} "
                        f"for app='{app_name}': {resp.text[:100]}"
                    )
        except Exception as exc:
            logger.debug(f"[Notifier] Slack send failed for '{app_name}': {exc}")

    # ── NATS background listener ──────────────────────────────────────────────

    def start_background(self, nats_client: Any) -> None:
        """Subscribe to NATS healing action subjects and auto-notify."""
        self._running  = True
        self._nats_client = nats_client
        self._nats_task   = asyncio.create_task(self._listen())

    def stop(self) -> None:
        """Stop the background NATS listener."""
        self._running = False
        if self._nats_task:
            self._nats_task.cancel()

    async def _listen(self) -> None:
        """Background loop: subscribe to nexus.actions.* and nexus.prescale.*"""
        try:
            nc = self._nats_client
            await nc.subscribe("nexus.actions.*", cb=self._on_action_message)
            await nc.subscribe("nexus.prescale.*", cb=self._on_prescale_message)
            logger.info("[Notifier] Subscribed to NATS nexus.actions.* + nexus.prescale.*")
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(f"[Notifier] NATS listener error: {exc}")

    async def _on_action_message(self, msg: Any) -> None:
        try:
            data     = json.loads(msg.data.decode())
            app_name = data.get("app") or data.get("resource_name", "unknown")
            await self.notify_heal(
                app_name    = app_name,
                runbook_id  = data.get("runbook_id", ""),
                outcome     = data.get("outcome", "unknown"),
                description = data.get("description", "Healing action taken"),
                target      = data.get("target"),
            )
        except Exception as exc:
            logger.debug(f"[Notifier] action message error: {exc}")

    async def _on_prescale_message(self, msg: Any) -> None:
        try:
            data = json.loads(msg.data.decode())
            await self.notify_prescale(
                app_name      = data.get("app", "unknown"),
                deployment    = data.get("deployment", "unknown"),
                current_rps   = data.get("current_rps", 0),
                predicted_rps = data.get("predicted_rps", 0),
                horizon_min   = data.get("horizon_min", 10),
                confidence    = data.get("confidence", 0),
                tables        = data.get("tables", []),
            )
        except Exception as exc:
            logger.debug(f"[Notifier] prescale message error: {exc}")
