"""
Pending incident verification and lifecycle event tracking for FeedbackLoop.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class PendingIncidentVerification:
    """
    Tracks an action's incident_id awaiting resolution verification.
    If the incident doesn't re-appear within the verify window, it's marked resolved.
    """

    __slots__ = ("incident_id", "runbook_id", "action_type", "outcome", "action_time")

    def __init__(
        self,
        incident_id: str,
        runbook_id: str,
        action_type: str,
        outcome: str,
    ) -> None:
        self.incident_id = incident_id
        self.runbook_id = runbook_id
        self.action_type = action_type
        self.outcome = outcome
        self.action_time = time.monotonic()

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "runbook_id": self.runbook_id,
            "action_type": self.action_type,
            "outcome": self.outcome,
            "action_time": self.action_time,
        }


async def handle_action_event(
    data: dict[str, Any],
    kb: Any,
    pending_verifications: dict[str, PendingIncidentVerification],
) -> None:
    """Record signal pattern and schedule incident verification if appropriate."""
    try:
        signal_type = data.get("signal_type")
        runbook_id = data.get("runbook_id")
        outcome = data.get("outcome", "")
        incident_id = data.get("incident_id")
        action_type = data.get("action_type", "unknown")

        if not signal_type or not runbook_id:
            return

        success = outcome in ("success", "rolled_back")
        await kb.record_pattern(
            signal_types={signal_type},
            runbook_id=runbook_id,
            success=success,
        )

        if incident_id and outcome in ("success", "rolled_back"):
            pending_verifications[incident_id] = PendingIncidentVerification(
                incident_id=incident_id,
                runbook_id=runbook_id,
                action_type=action_type,
                outcome=outcome,
            )
            logger.debug(
                f"[FeedbackLoop] Scheduled verification for incident {incident_id} "
                f"(runbook={runbook_id}, outcome={outcome})"
            )
    except Exception as exc:
        logger.debug(f"[FeedbackLoop] action pattern error: {exc}")


async def handle_incident_event(
    data: dict[str, Any],
    recent_incidents: dict[str, float],
    pending_verifications: dict[str, PendingIncidentVerification],
    kb: Any,
) -> None:
    """Track incoming incidents to detect re-appearance of healed clusters."""
    try:
        incident_id = data.get("correlation_id") or data.get("incident_id")
        if incident_id:
            recent_incidents[incident_id] = time.monotonic()

            if incident_id in pending_verifications:
                pv = pending_verifications.pop(incident_id)
                await kb.record_incident_outcome(
                    incident_id=incident_id,
                    runbook_id=pv.runbook_id,
                    action_type=pv.action_type,
                    resolved=False,
                    reason="re-fired",
                )
                logger.debug(
                    f"[FeedbackLoop] Incident {incident_id} re-fired — "
                    f"verification FAILED (runbook={pv.runbook_id})"
                )
    except Exception as exc:
        logger.debug(f"[FeedbackLoop] incident tracking error: {exc}")


def handle_lifecycle_event(
    data: dict[str, Any],
    subject: str,
    recent_incidents: dict[str, float],
    pending_verifications: dict[str, PendingIncidentVerification],
) -> None:
    """Clear tracking on terminal lifecycle events."""
    try:
        incident_id = data.get("incident_id")
        if not incident_id:
            return
        pending_verifications.pop(incident_id, None)
        recent_incidents.pop(incident_id, None)
        logger.debug(
            f"[FeedbackLoop] Lifecycle {subject} for incident {incident_id} — tracking cleared"
        )
    except Exception as exc:
        logger.debug(f"[FeedbackLoop] lifecycle event error: {exc}")


async def verify_pending_incidents(
    pending_verifications: dict[str, PendingIncidentVerification],
    kb: Any,
    verify_window_s: float,
) -> None:
    """Mark expired unrefired incidents as resolved."""
    now = time.monotonic()
    expired: list[str] = []

    for incident_id, pv in pending_verifications.items():
        elapsed = now - pv.action_time
        if elapsed >= verify_window_s:
            expired.append(incident_id)
            await kb.record_incident_outcome(
                incident_id=incident_id,
                runbook_id=pv.runbook_id,
                action_type=pv.action_type,
                resolved=True,
                reason="verified",
            )
            logger.debug(
                f"[FeedbackLoop] Incident {incident_id} verified RESOLVED "
                f"after {verify_window_s}s (runbook={pv.runbook_id})"
            )

    for iid in expired:
        pending_verifications.pop(iid, None)

    if expired:
        logger.info(
            f"[FeedbackLoop] Verification check: {len(expired)} incident(s) resolved, "
            f"{len(pending_verifications)} still pending"
        )
