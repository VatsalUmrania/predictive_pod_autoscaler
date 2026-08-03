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
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

# Slack interactivity URL — when set, `notify_approval_required` emits
# functional Approve/Reject buttons that POST to /slack/interactive on the
# status API (see nexus.observability.status_api). The same URL is used by
# chatops.send_approval_request for the LLM path. Defaults to "" (disabled).
SLACK_INTERACTIVE_URL = os.environ.get("SLACK_INTERACTIVE_URL", "")

# Slack app signing secret (Settings → Basic Information → App Credentials).
# Required to authenticate interactive payloads POSTed to /slack/interactive —
# without it the endpoint 401s every click (secure-by-default).
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
# Reject interactive requests whose timestamp is older than this (replay guard).
SLACK_REQUEST_TOLERANCE_S = 60 * 5

def verify_slack_request(
    raw_body: bytes,
    headers,
    *,
    signing_secret: str | None = None,
    tolerance_s: int | None = None,
) -> bool:
    """Validate a Slack request signature (HMAC-SHA256, ``v0`` scheme).

    Trust-boundary gate for the /slack/interactive endpoint. ``False`` MUST
    mean "reject the request": any missing header, unset secret, stale
    timestamp, or signature mismatch returns False and never raises, so the
    caller can treat the boolean as a hard auth gate.

    Refs: https://api.slack.com/authentication/verifying-requests-from-slack
    """
    secret = signing_secret if signing_secret is not None else SLACK_SIGNING_SECRET
    if not secret:
        return False
    ts = headers.get("X-Slack-Request-Timestamp")
    sig = headers.get("X-Slack-Signature")
    if not ts or not sig:
        return False
    try:
        ts_int = int(ts)
    except (TypeError, ValueError):
        return False
    tol = tolerance_s if tolerance_s is not None else SLACK_REQUEST_TOLERANCE_S
    if abs(time.time() - ts_int) > tol:
        return False
    # Signing base: b"v0:<timestamp>:<raw body>". Concatenate raw_body bytes so
    # no decode/reencode alters the exact wire bytes Slack signed.
    base = b"v0:" + str(ts).encode() + b":" + raw_body
    expected = "v0=" + hashlib.sha256(base).hexdigest()
    # Constant-time compare guards the prefix; the digest length is fixed.
    return hmac.compare_digest(expected, str(sig))

def resolve_slack_webhook(app_name: str | None) -> str | None:
    """Resolve the Slack webhook URL to use for ``app_name`` (single source).

    A per-app ``selfheal.yaml`` override (``notifications.slack_webhook``) wins
    when that app has a policy loaded — including an explicit ``null`` (that app
    is deliberately disabled and stays silent over the global fallback). When
    the app has NO policy loaded at all, fall back to the global ``SLACK_WEBHOOK``
    env var, so notifications work out-of-the-box without mounting a
    selfheal.yaml. Returns None (caller silently skips) when neither resolves.

    Both the governed Notifier path and chatops.send_approval_request go through
    this — one env var (``SLACK_WEBHOOK``), no SLACK_WEBHOOK_URL duplication.
    """
    if app_name:
        try:
            from nexus.integration.dashboard import _policy_cache

            # App in cache → its policy is authoritative, even if webhook is null
            # (a disabled app must not be resurrected by the global fallback).
            if app_name in _policy_cache:
                cfg = _policy_cache[app_name] or {}
                return (cfg.get("notifications") or {}).get("slack_webhook") or None
        except Exception:
            pass
    return os.environ.get("SLACK_WEBHOOK") or None

def _approval_buttons_block(approval_id: str) -> dict:
    """Slack Block Kit action block with Approve/Reject buttons.

    The buttons POST to SLACK_INTERACTIVE_URL with a payload the
    /slack/interactive endpoint parses. If SLACK_INTERACTIVE_URL is empty,
    returns an empty block (caller must filter) — this keeps the notifier's
    Slack message renderable even without the interactive endpoint configured.
    """
    if not SLACK_INTERACTIVE_URL:
        return {"type": "context", "elements": [{"type": "mrkdwn", "text": ""}]}
    return {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Approve ✅"},
                "style": "primary",
                "action_id": "nexus_approve",
                "value": approval_id,
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Reject ❌"},
                "style": "danger",
                "action_id": "nexus_reject",
                "value": approval_id,
            },
        ],
    }

class Notifier:
    """
    Sends Slack notifications for NEXUS healing events.

    Webhook URLs come from the app's selfheal.yaml policy cache.
    If an app has no slack_webhook configured, notifications are silently skipped.
    """

    def __init__(self) -> None:
        self._fail_counts: dict[str, int] = {}  # app_name → consecutive failures
        self._nats_task: asyncio.Task | None = None
        self._running: bool = False

    #  Public notification API

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
        if not webhook:
            return

        icon = "✅" if outcome == "success" else ("❌" if outcome == "failed" else "↩️")
        color = "#36a64f" if outcome == "success" else "#e01e5a"

        payload = {
            "attachments": [
                {
                    "color": color,
                    "fallback": f"NEXUS healed {app_name}: {description}",
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"*{icon} NEXUS Healing Action — `{app_name}`*",
                            },
                        },
                        {
                            "type": "section",
                            "fields": [
                                {"type": "mrkdwn", "text": f"*Action:*\n{description}"},
                                {"type": "mrkdwn", "text": f"*Outcome:*\n`{outcome}`"},
                                {
                                    "type": "mrkdwn",
                                    "text": f"*Runbook:*\n`{runbook_id}`",
                                },
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

        await self._send(webhook, payload, app_name)

        # Track failures for escalation
        if outcome in ("failed", "rolled_back"):
            self._fail_counts[app_name] = self._fail_counts.get(app_name, 0) + 1
            threshold = self._get_page_threshold(app_name)
            if self._fail_counts[app_name] >= threshold:
                await self.notify_escalation(app_name, self._fail_counts[app_name])
                self._fail_counts[app_name] = 0  # reset after escalation
        else:
            self._fail_counts[app_name] = 0  # reset on success

    async def notify_prescale(
        self,
        app_name: str,
        deployment: str,
        current_rps: float,
        predicted_rps: float,
        horizon_min: int = 10,
        confidence: float = 0.0,
        tables: list = None,
    ) -> None:
        """Send a pre-scale prediction notification."""
        webhook = self._get_webhook(app_name)
        if not webhook:
            return

        tables = tables or []
        mult = predicted_rps / current_rps if current_rps > 0 else 1.0
        table_clause = (
            f" — DB spike on {', '.join(f'`{t}`' for t in tables[:3])}"
            if tables
            else ""
        )

        payload = {
            "attachments": [
                {
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
                }
            ]
        }

        await self._send(webhook, payload, app_name)

    async def notify_escalation(
        self,
        app_name: str,
        failed_attempts: int,
    ) -> None:
        """Send an SRE escalation alert when auto-healing is exhausted."""
        webhook = self._get_webhook(app_name)
        if not webhook:
            return

        payload = {
            "attachments": [
                {
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
                }
            ]
        }

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
        """Send a notification when an L3 action requires human approval."""
        webhook = self._get_webhook(app_name)
        if not webhook:
            return

        # Extract RCA info from context for better Slack message
        rca = (context or {}).get("rca", {})
        event = (context or {}).get("event", {})
        action = (context or {}).get("action", {})
        rca_source = (context or {}).get("rca_source", "unknown")

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*✋ NEXUS L{healing_level} Action Requires Approval*",
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Runbook:*\n`{runbook_id}`"},
                    {"type": "mrkdwn", "text": f"*Target:*\n`{target}`"},
                    {
                        "type": "mrkdwn",
                        "text": f"*Confidence:*\n`{confidence:.2f}`",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Approval ID:*\n`{approval_id}`",
                    },
                ],
            },
        ]

        # Add RCA diagnosis if available
        if rca:
            root_cause = rca.get("root_cause", "Unknown")
            failure_class = rca.get("failure_class", "Unknown")
            reasoning = rca.get("reasoning", "")
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*RCA Diagnosis:*\n"
                            f"*Class:* {failure_class}\n"
                            f"*Root Cause:* {root_cause[:300]}{'...' if len(root_cause) > 300 else ''}"
                        ),
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

        # Add signal info from event
        if event:
            signal_type = event.get("signal_type", "unknown")
            severity = event.get("severity", "unknown")
            namespace = event.get("namespace", "unknown")
            blocks.append(
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Signal:*\n`{signal_type}`"},
                        {"type": "mrkdwn", "text": f"*Severity:*\n`{severity}`"},
                        {"type": "mrkdwn", "text": f"*Namespace:*\n`{namespace}`"},
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
        blocks.append(_approval_buttons_block(approval_id))

        payload = {"attachments": [{"color": "#ff9900", "fallback": f"NEXUS L{healing_level} Action Requires Approval: {runbook_id} for {target}", "blocks": blocks}]}
        await self._send(webhook, payload, app_name)

    # Policy helpers
    def _get_webhook(self, app_name: str) -> str | None:
        """Return the Slack webhook URL for an app, or None if not configured.

        Delegates to ``resolve_slack_webhook`` so every notification path
        (heal / prescale / escalation / approval) uses a single resolver with
        the ``SLACK_WEBHOOK`` env fallback. Per-app ``selfheal.yaml`` overrides
        still win.
        """
        return resolve_slack_webhook(app_name)

    def _get_page_threshold(self, app_name: str) -> int:
        """Return the page_sre_after threshold for an app (default 3)."""
        try:
            from nexus.integration.dashboard import _policy_cache

            cfg = _policy_cache.get(app_name, {})
            return cfg.get("notifications", {}).get("page_sre_after", 3)
        except Exception:
            return 3

    # HTTP send
    async def _send(
        self,
        webhook_url: str,
        payload: dict[str, Any],
        app_name: str,
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
                        f"[Notifier] Slack webhook returned {resp.status_code} "
                        f"for app='{app_name}': {resp.text[:100]}"
                    )
        except Exception as exc:
            logger.debug(f"[Notifier] Slack send failed for '{app_name}': {exc}")

    # NATS background listener
    def start_background(self, nats_client: Any) -> None:
        """Subscribe to NATS healing action subjects and auto-notify."""
        self._running = True
        self._nats_client = nats_client
        self._nats_task = asyncio.create_task(self._listen())
        self._nats_task.add_done_callback(self._on_listen_done)

    def _on_listen_done(self, task: asyncio.Task) -> None:
        """If the background loop crashed (not cancelled), surface and restart once."""
        if self._running and not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.error(f"[Notifier] _listen crashed: {exc} — restarting in 5s")
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
        """Background loop: subscribe to nexus.actions.* and nexus.prescale.*.

        Retries the subscription with exponential backoff until both succeed,
        rather than parking silently when the broker wasn't ready. Survives
        CancelledError on shutdown.
        """
        nc = self._nats_client

        async def _action_wrapper(data: dict, subject: str) -> None:
            await self._on_action_message(data)

        async def _prescale_wrapper(data: dict, subject: str) -> None:
            await self._on_prescale_message(data)

        async def _approval_wrapper(data: dict, subject: str) -> None:
            await self._on_approval_message(data)

        # Retry both subscriptions until both succeed, with capped backoff
        action_ok = False
        prescale_ok = False
        approval_ok = False
        backoff = 1.0
        while self._running and not (action_ok and prescale_ok and approval_ok):
            if not action_ok:
                try:
                    await nc.subscribe_raw("nexus.actions.>", handler=_action_wrapper)
                    action_ok = True
                except Exception as exc:
                    logger.warning(f"[Notifier] nexus.actions.> subscribe retry: {exc}")
            if not prescale_ok:
                try:
                    await nc.subscribe_raw("nexus.prescale.>", handler=_prescale_wrapper)
                    prescale_ok = True
                except Exception as exc:
                    logger.warning(
                        f"[Notifier] nexus.prescale.> subscribe retry: {exc}"
                    )
            if not approval_ok:
                try:
                    await nc.subscribe_raw(
                        "nexus.approvals.>", handler=_approval_wrapper
                    )
                    approval_ok = True
                except Exception as exc:
                    logger.warning(
                        f"[Notifier] nexus.approvals.> subscribe retry: {exc}"
                    )

            if not (action_ok and prescale_ok and approval_ok):
                await asyncio.sleep(min(backoff, 30.0))
                backoff *= 2
            else:
                logger.info(
                    "[Notifier] Subscribed to NATS nexus.actions.> + nexus.prescale.> + nexus.approvals.>"
                )

        # Park until stopped. If the NATS connection is severed the
        # NATSClient itself handles reconnect; subscription tasks survive
        # because they are JS consumers with the standard client.
        while self._running:
            await asyncio.sleep(1)

    async def _on_action_message(self, data: dict) -> None:
        try:
            app_name = data.get("app") or data.get("resource_name", "unknown")
            await self.notify_heal(
                app_name=app_name,
                runbook_id=data.get("runbook_id", ""),
                outcome=data.get("outcome", "unknown"),
                description=data.get("description", "Healing action taken"),
                target=data.get("target"),
            )
        except Exception as exc:
            logger.debug(f"[Notifier] action message error: {exc}")

    async def _on_prescale_message(self, data: dict) -> None:
        try:
            await self.notify_prescale(
                app_name=data.get("app", "unknown"),
                deployment=data.get("deployment", "unknown"),
                current_rps=data.get("current_rps", 0),
                predicted_rps=data.get("predicted_rps", 0),
                horizon_min=data.get("horizon_min", 10),
                confidence=data.get("confidence", 0),
                tables=data.get("tables", []),
            )
        except Exception as exc:
            logger.debug(f"[Notifier] prescale message error: {exc}")

    async def _on_approval_message(self, data: dict) -> None:
        try:
            await self.notify_approval_required(
                app_name=data.get("app", "unknown"),
                approval_id=data.get("approval_id", ""),
                runbook_id=data.get("runbook_id", ""),
                target=data.get("target", ""),
                healing_level=data.get("healing_level", 3),
                confidence=data.get("confidence", 0.0),
                context=data.get("context", {}),
            )
        except Exception as exc:
            logger.debug(f"[Notifier] approval message error: {exc}")
