"""
NATS subscription listener and message dispatching for Notifier.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def handle_action_message(notifier: Any, data: dict[str, Any]) -> None:
    try:
        app_name = data.get("app") or data.get("resource_name", "unknown")
        await notifier.notify_heal(
            app_name=app_name,
            runbook_id=data.get("runbook_id", ""),
            outcome=data.get("outcome", "unknown"),
            description=data.get("description", "Healing action taken"),
            target=data.get("target"),
        )
    except Exception as exc:
        logger.debug(f"[Notifier] action message error: {exc}")


async def handle_prescale_message(notifier: Any, data: dict[str, Any]) -> None:
    try:
        await notifier.notify_prescale(
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


async def handle_approval_message(notifier: Any, data: dict[str, Any]) -> None:
    try:
        await notifier.notify_approval_required(
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


async def handle_incident_resolved_message(notifier: Any, data: dict[str, Any]) -> None:
    try:
        app_name = (
            data.get("app")
            or data.get("namespace")
            or data.get("target_name")
            or "default"
        )
        target = (
            data.get("target") or f"{app_name}/{data.get('target_name', 'resource')}"
        )
        rca = data.get("rca") or data.get("diagnosis") or {}
        verif = data.get("verification") or {}
        plan = data.get("plan") or {}

        failure_class = (
            rca.get("failure_class") or plan.get("failure_mode") or "Unknown"
        )
        root_cause = (
            rca.get("root_cause")
            or rca.get("reasoning")
            or "Target restored to healthy baseline."
        )

        steps = plan.get("steps") or []
        action_taken = (
            steps[0].get("description")
            if steps and steps[0].get("description")
            else data.get("action_taken")
            or data.get("description")
            or "Remediation plan executed"
        )

        verif_details = (
            verif.get("details") or "Target verified healthy and SLO restored"
        )
        conf = float(rca.get("confidence") or data.get("confidence") or 1.0)
        dur = data.get("duration_s")

        await notifier.notify_incident_resolved(
            app_name=app_name,
            incident_id=data.get("incident_id", "unknown"),
            target=str(target),
            failure_class=failure_class,
            root_cause=root_cause,
            action_taken=action_taken,
            verification_details=verif_details,
            confidence=conf,
            duration_s=dur,
            context=data,
        )
    except Exception as exc:
        logger.debug(f"[Notifier] incident resolved message error: {exc}")


async def handle_incident_escalated_message(
    notifier: Any, data: dict[str, Any]
) -> None:
    try:
        app_name = (
            data.get("app")
            or data.get("namespace")
            or data.get("target_name")
            or "default"
        )
        target = (
            data.get("target") or f"{app_name}/{data.get('target_name', 'resource')}"
        )
        rca = data.get("rca") or data.get("diagnosis") or {}
        reason = (
            data.get("reason")
            or data.get("error_message")
            or "Remediation failed or maximum retries exhausted"
        )
        failure_class = rca.get("failure_class") or "Unknown"
        root_cause = rca.get("root_cause") or rca.get("reasoning") or "Unknown"

        await notifier.notify_incident_escalated(
            app_name=app_name,
            incident_id=data.get("incident_id", "unknown"),
            target=str(target),
            reason=reason,
            failure_class=failure_class,
            root_cause=root_cause,
            context=data,
        )
    except Exception as exc:
        logger.debug(f"[Notifier] incident escalated message error: {exc}")


async def handle_report_ready_message(notifier: Any, data: dict[str, Any]) -> None:
    try:
        app_name = data.get("app") or data.get("namespace") or "default"
        await notifier.notify_report_ready(
            app_name=app_name,
            incident_id=data.get("incident_id", "unknown"),
            target=str(data.get("target") or "unknown"),
            outcome=data.get("outcome") or "resolved",
            summary=data.get("summary") or "",
            report_url=data.get("report_url") or "",
        )
    except Exception as exc:
        logger.debug(f"[Notifier] report ready message error: {exc}")


async def listen_notifier_nats(notifier: Any, nc: Any) -> None:
    """Background loop: subscribe to action, prescale, approval, and lifecycle topics."""

    async def _action_wrapper(data: dict, subject: str) -> None:
        await notifier._on_action_message(data)

    async def _prescale_wrapper(data: dict, subject: str) -> None:
        await notifier._on_prescale_message(data)

    async def _approval_wrapper(data: dict, subject: str) -> None:
        await notifier._on_approval_message(data)

    async def _incident_wrapper(data: dict, subject: str) -> None:
        if "resolved" in subject:
            await notifier._on_incident_resolved_message(data)
        elif "escalated" in subject:
            await notifier._on_incident_escalated_message(data)
        elif "report" in subject:
            await notifier._on_report_ready_message(data)

    action_ok = prescale_ok = approval_ok = incident_ok = False
    backoff = 1.0
    while notifier._running and not (
        action_ok and prescale_ok and approval_ok and incident_ok
    ):
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
                logger.warning(f"[Notifier] nexus.prescale.> subscribe retry: {exc}")
        if not approval_ok:
            try:
                await nc.subscribe_raw("nexus.approvals.>", handler=_approval_wrapper)
                approval_ok = True
            except Exception as exc:
                logger.warning(f"[Notifier] nexus.approvals.> subscribe retry: {exc}")
        if not incident_ok:
            try:
                await nc.subscribe_raw("nexus.lifecycle.>", handler=_incident_wrapper)
                incident_ok = True
            except Exception as exc:
                logger.warning(f"[Notifier] nexus.lifecycle.> subscribe retry: {exc}")

        if not (action_ok and prescale_ok and approval_ok and incident_ok):
            await asyncio.sleep(min(backoff, 30.0))
            backoff *= 2
        else:
            logger.info("[Notifier] Subscribed to NATS notification topics")

    while notifier._running:
        await asyncio.sleep(1)
