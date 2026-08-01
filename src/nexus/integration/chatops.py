"""
ChatOps Integration
===================
Handles sending messages to and receiving callbacks from Slack (or other ChatOps platforms)
for the human-in-the-loop approval workflow.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from nexus.integration.notifier import SLACK_INTERACTIVE_URL, _approval_buttons_block

logger = logging.getLogger(__name__)

# Assumes SLACK_WEBHOOK_URL is set in the environment
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")

async def send_approval_request(
    diagnosis: str, context: dict[str, Any], approval_id: str | None = None
) -> bool:
    """
    Sends a diagnosis and proposed remediation to Slack with Approve/Reject buttons.

    If `approval_id` is provided and `SLACK_INTERACTIVE_URL` is configured,
    the message includes functional buttons that POST to the FastAPI endpoint.
    Falls back to CLI instructions (`nexus approve <id>`) when not configured.
    """
    if not SLACK_WEBHOOK_URL:
        logger.warning("SLACK_WEBHOOK_URL not set. Skipping ChatOps notification.")
        logger.info(f"Simulating sending approval request:\nDiagnosis: {diagnosis}\nContext: {context}")
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
            response = await client.post(SLACK_WEBHOOK_URL, json={"blocks": blocks})
            response.raise_for_status()
            logger.info("Successfully sent approval request to Slack.")
            return True

    except Exception as e:
        logger.error(f"Error sending Slack notification: {e}")
        return False
