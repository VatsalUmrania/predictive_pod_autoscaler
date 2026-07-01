"""
Escalate Node
==============
Sends a Slack notification and marks the incident as escalated.

Called in two situations:
  1. Policy blocked the action (confidence too low or action not whitelisted)
     → "Human approval required" message
  2. Verify confirmed the remediation didn't work after max_retries
     → "Manual action required" message

This node ALWAYS succeeds (even if Slack is unreachable — it logs and continues).
"""

from __future__ import annotations

import logging

from aws.config import AgentConfig
from aws.graph.state import AgentState
from aws.tools import slack

logger = logging.getLogger(__name__)


def escalate(state: AgentState) -> dict:
    """
    LangGraph node: notify Slack and mark incident as escalated.

    Input state fields used:  resource_name, signal_type, rca, action_result,
                              approved, policy_reason, resolved, retry_count
    Output state fields set:  escalated, slack_sent
    """
    cfg = AgentConfig.load()
    resource = state.get("resource_name", "unknown")
    signal = state.get("signal_type", "unknown")
    rca = state.get("rca") or {}
    approved = state.get("approved", True)
    policy_reason = state.get("policy_reason", "")
    resolved = state.get("resolved", False)
    retry_count = state.get("retry_count", 0)

    root_cause = rca.get("root_cause", "Unknown")
    action = rca.get("action", "alert_only")
    params = rca.get("params", {})
    confidence = float(rca.get("confidence", 0.0))

    # Determine the escalation scenario
    if not approved:
        # Scenario 1: Policy blocked → human approval request
        logger.warning(f"[Escalate] Sending approval request for {resource}: {policy_reason}")
        sent = slack.send_approval_request(
            resource_name=resource,
            action=action,
            params=params,
            root_cause=root_cause,
            confidence=confidence,
            webhook_url=cfg.slack_webhook_url,
        )
        message_type = "approval_request"

    elif not resolved and retry_count >= cfg.max_retries:
        # Scenario 2: Remediation failed after retries → manual action needed
        logger.warning(f"[Escalate] Remediation failed after {retry_count} attempts for {resource}")
        sent = slack.send_incident_alert(
            resource_name=resource,
            signal_type=signal,
            root_cause=root_cause,
            action_taken=f"{action} (attempted {retry_count}x)",
            confidence=confidence,
            resolved=False,
            webhook_url=cfg.slack_webhook_url,
        )
        message_type = "remediation_failed"

    else:
        # Scenario 3: alert_only action or general alert
        logger.info(f"[Escalate] Sending alert for {resource}")
        sent = slack.send_incident_alert(
            resource_name=resource,
            signal_type=signal,
            root_cause=root_cause,
            action_taken=action,
            confidence=confidence,
            resolved=False,
            webhook_url=cfg.slack_webhook_url,
        )
        message_type = "alert"

    logger.info(f"[Escalate] {message_type} sent={sent} for {resource}")
    return {"escalated": True, "slack_sent": sent}


def notify_resolved(state: AgentState) -> dict:
    """
    Send a resolution notification to Slack.
    Called at the end of a successful remediation (resolved=True).
    """
    cfg = AgentConfig.load()
    resource = state.get("resource_name", "unknown")
    signal = state.get("signal_type", "unknown")
    rca = state.get("rca") or {}

    root_cause = rca.get("root_cause", "Unknown")
    action = rca.get("action", "unknown")
    confidence = float(rca.get("confidence", 0.0))

    logger.info(f"[Escalate] Sending resolution notification for {resource}")
    sent = slack.send_incident_alert(
        resource_name=resource,
        signal_type=signal,
        root_cause=root_cause,
        action_taken=action,
        confidence=confidence,
        resolved=True,
        webhook_url=cfg.slack_webhook_url,
    )
    return {"slack_sent": sent, "escalated": False}
