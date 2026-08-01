"""
ChatOps Integration
===================
Handles sending messages to and receiving callbacks from Slack (or other ChatOps platforms)
for the human-in-the-loop approval workflow.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Assumes SLACK_WEBHOOK_URL is set in the environment
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")

async def send_approval_request(diagnosis: str, context: Dict[str, Any]) -> bool:
    """
    Sends a diagnosis and proposed remediation to Slack with Approve/Reject buttons.
    """
    if not SLACK_WEBHOOK_URL:
        logger.warning("SLACK_WEBHOOK_URL not set. Skipping ChatOps notification.")
        # For testing, we might want to automatically approve or just log
        logger.info(f"Simulating sending approval request:\nDiagnosis: {diagnosis}\nContext: {context}")
        return True
        
    try:
        import httpx
        
        # Construct Slack Block Kit message
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
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Approve Fix"
                        },
                        "style": "primary",
                        "value": "approve"
                    },
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Reject"
                        },
                        "style": "danger",
                        "value": "reject"
                    }
                ]
            }
        ]
        
        async with httpx.AsyncClient() as client:
            response = await client.post(SLACK_WEBHOOK_URL, json={"blocks": blocks})
            response.raise_for_status()
            logger.info("Successfully sent approval request to Slack.")
            return True
            
    except Exception as e:
        logger.error(f"Error sending Slack notification: {e}")
        return False

# In a real implementation, we would also have an endpoint handler here 
# (e.g., FastAPI route) to receive the interactive callback from Slack when 
# the user clicks "Approve" or "Reject". For this iteration, we focus on sending the request.
