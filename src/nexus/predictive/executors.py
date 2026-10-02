"""Execution strategies for Prescaler modes (Shadow, Advisory, Autonomous)."""
from __future__ import annotations

import json
import logging
from typing import Any

from nexus.bus.incident_event import AgentType, IncidentEvent, Severity, SignalType
from nexus.governance.action_ladder import ActionLadder
from nexus.predictive.models import PrescaleDecision

logger = logging.getLogger(__name__)


async def execute_advisory(decision: PrescaleDecision, source_event: IncidentEvent, nats_client: Any) -> None:
    """Publish a NATS advisory event for human operator approval."""
    advisory_event = IncidentEvent(
        agent=AgentType.ORCHESTRATOR,
        signal_type=SignalType.ANOMALY_PREDICTED,
        severity=Severity.WARNING,
        namespace=decision.namespace,
        resource_name=decision.deployment_name,
        correlation_id=source_event.correlation_id or source_event.event_id,
        context={
            "type": "pre_scale_advisory",
            "decision_id": decision.decision_id,
            "deployment": decision.deployment_name,
            "namespace": decision.namespace,
            "current_replicas": decision.current_replicas,
            "recommended_replicas": decision.recommended_replicas,
            "current_rps": decision.current_rps,
            "predicted_rps": decision.predicted_rps,
            "horizon_minutes": decision.horizon_minutes,
            "confidence": decision.confidence,
            "db_table_trigger": decision.db_table_trigger,
            "action": "Run: nexus prescale approve " + decision.decision_id,
        },
        confidence=decision.confidence,
    )
    await nats_client.publish(advisory_event)


async def execute_autonomous(
    decision: PrescaleDecision,
    source_event: IncidentEvent,
    ladder: ActionLadder | None,
    cooldown: float,
) -> None:
    """Scale deployment directly via ActionLadder (L2 action)."""
    if ladder is None:
        logger.error("[Prescaler] AUTONOMOUS mode requires ActionLadder — not configured")
        return

    scale_action = {
        "type": "scale_deployment",
        "description": f"Pre-scale {decision.deployment_name}",
        "params": {
            "namespace": decision.namespace,
            "name": decision.deployment_name,
            "replicas": decision.recommended_replicas,
        },
    }
    ladder_dec = await ladder.evaluate(
        action=scale_action,
        event=source_event,
        target=f"{decision.namespace}/{decision.deployment_name}",
        confidence=decision.confidence,
        action_type="scale_deployment",
        healing_level=2,
    )
    if not ladder_dec.can_proceed:
        decision.outcome = "governance_blocked"
        return

    try:
        from kubernetes import client as k8s_client
        from kubernetes import config as k8s_config

        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()

        apps_v1 = k8s_client.AppsV1Api()
        patch = {"spec": {"replicas": decision.recommended_replicas}}
        apps_v1.patch_namespaced_deployment_scale(decision.deployment_name, decision.namespace, patch)
        await ladder.set_cooldown(
            "scale_deployment",
            f"{decision.namespace}/{decision.deployment_name}",
            cooldown_seconds=int(cooldown),
        )
        decision.executed = True
        decision.outcome = "executed"
    except Exception as exc:
        decision.outcome = "failed"
        logger.error(f"[Prescaler] [AUTONOMOUS] Scale failed: {exc}")


async def publish_prescale_decision(decision: PrescaleDecision, source_event: IncidentEvent, nats_client: Any) -> None:
    """Publish decision to nexus.prescale.* for Notifier/dashboard."""
    subject = f"nexus.prescale.{decision.deployment_name}.{decision.mode.value}"
    payload = {
        "type": "prescale_decision",
        "decision_id": decision.decision_id,
        "deployment": decision.deployment_name,
        "namespace": decision.namespace,
        "mode": decision.mode.value,
        "executed": decision.executed,
        "outcome": decision.outcome,
        "current_replicas": decision.current_replicas,
        "recommended_replicas": decision.recommended_replicas,
        "current_rps": decision.current_rps,
        "predicted_rps": decision.predicted_rps,
        "horizon_minutes": decision.horizon_minutes,
        "confidence": decision.confidence,
        "db_table_trigger": decision.db_table_trigger,
        "incident_id": source_event.correlation_id or source_event.event_id,
        "timestamp": decision.decided_at,
    }
    try:
        js = getattr(nats_client, "_js", None)
        if js is not None:
            await js.publish(subject, json.dumps(payload).encode())
    except Exception:
        pass
