"""
ChatOps Integration
===================
Handles sending messages to and receiving callbacks from Slack (or other ChatOps platforms)
for the human-in-the-loop approval workflow.
"""

from __future__ import annotations

import logging
from typing import Any

from nexus.integration.notifier import (
    SLACK_INTERACTIVE_URL,
    _approval_buttons_block,
    resolve_slack_webhook,
)

logger = logging.getLogger(__name__)

async def send_approval_request(
    diagnosis: str,
    context: dict[str, Any],
    approval_id: str | None = None,
    app_name: str | None = None,
) -> bool:
    """Send a diagnosis + proposed remediation to Slack with Approve/Reject buttons.

    Webhook is resolved via ``resolve_slack_webhook(app_name)``: a per-app
    selfheal.yaml override wins, else the global ``SLACK_WEBHOOK`` env fallback.
    If ``approval_id`` is provided and ``SLACK_INTERACTIVE_URL`` is configured,
    the message carries functional buttons that POST to /slack/interactive;
    otherwise it falls back to CLI instructions (``nexus approve <id>``).
    """
    webhook = resolve_slack_webhook(app_name)
    if not webhook:
        logger.warning(f"Slack webhook not configured for app={app_name!r}. Skipping.")
        return True

    try:
        import httpx

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "NEXUS Self-Healing: Approval Required"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Context:*\n```{context}```"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Diagnosis & Proposal:*\n{diagnosis}"
                }
            },
        ]

        if approval_id:
            blocks.append(_approval_buttons_block(approval_id))
            if not SLACK_INTERACTIVE_URL:
                blocks.append(
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"To approve this action, run:\n`nexus approve {approval_id}`",
                        }
                    }
                )

        async with httpx.AsyncClient() as client:
            response = await client.post(webhook, json={"blocks": blocks})
            response.raise_for_status()
            logger.info("Successfully sent approval request to Slack.")
            return True

    except Exception as e:
        logger.error(f"Error sending Slack notification: {e}")
        return False
