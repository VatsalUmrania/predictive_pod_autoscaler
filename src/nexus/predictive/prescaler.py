"""NEXUS Prescaler Coordinator.

Receives TRAFFIC_SPIKE_PREDICTED events and decides whether to pre-scale
deployments before spikes materialize.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any

from nexus.bus.incident_event import AgentType, IncidentEvent, Severity, SignalType
from nexus.bus.nats_client import NATSClient
from nexus.governance.action_ladder import ActionLadder
from nexus.predictive.calculator import (
    compute_recommended_replicas,
    endpoint_to_deployment,
    parse_horizon,
)
from nexus.predictive.executors import (
    execute_advisory,
    execute_autonomous,
    publish_prescale_decision,
)
from nexus.predictive.models import PrescaleDecision, PrescaleMode
from nexus.predictive.tracker import PrecisionTracker

logger = logging.getLogger(__name__)


class Prescaler:
    """Pre-scale advisor and executor."""

    def __init__(
        self,
        nats_client: NATSClient,
        action_ladder: ActionLadder | None = None,
        mode: PrescaleMode | None = None,
        max_replicas: int | None = None,
        min_confidence: float | None = None,
        threshold_pct: float | None = None,
        cooldown_seconds: float | None = None,
        target_rps_per_replica: float = 100.0,
    ):
        self._nats = nats_client
        self._ladder = action_ladder
        self.mode = mode or PrescaleMode(os.getenv("NEXUS_PRESCALE_MODE", "shadow"))
        self._max_replicas = max_replicas or int(
            os.getenv("NEXUS_PRESCALE_MAX_REPLICAS", "20")
        )
        self._min_confidence = min_confidence or float(
            os.getenv("NEXUS_PRESCALE_MIN_CONFIDENCE", "0.55")
        )
        self._threshold_pct = threshold_pct or float(
            os.getenv("NEXUS_PRESCALE_THRESHOLD_PCT", "30")
        )
        self._cooldown = cooldown_seconds or float(
            os.getenv("NEXUS_PRESCALE_COOLDOWN_S", "300")
        )
        self._target_rps = target_rps_per_replica

        self._tracker = PrecisionTracker()
        self._all_decisions: list[PrescaleDecision] = []
        self._cooldowns: dict[str, float] = {}

        self._events_received = 0
        self._decisions_made = 0
        self._skipped_cooldown = 0
        self._skipped_confidence = 0
        self._skipped_threshold = 0

    async def subscribe_to_ppa_predictions(self) -> None:
        """Subscribe to ppa.predictions.* NATS subject."""

        async def handler(data: dict, subject: str) -> None:
            if not subject.startswith("ppa.predictions."):
                return
            deployment = data.get("deployment", subject.split(".")[-1])
            event = IncidentEvent(
                agent=AgentType.ORCHESTRATOR,
                signal_type=SignalType.TRAFFIC_SPIKE_PREDICTED,
                severity=Severity.WARNING,
                namespace=data.get("namespace", "default"),
                resource_name=deployment,
                context={
                    "current_rps": data.get("current_rps", 0.0),
                    "predicted_rps": data.get("predicted_rps", 0.0),
                    "confidence": data.get("confidence", 0.0),
                    "endpoint": f"/{deployment}",
                    "prediction_horizon_minutes": parse_horizon(
                        data.get("horizon_minutes", 10)
                    ),
                    "db_table_trigger": None,
                    "ppa_model_version": data.get("model_version", "unknown"),
                    "raw_features": data.get("raw_features", {}),
                },
                confidence=data.get("confidence", 0.0),
            )
            try:
                await self.handle_spike_prediction(event)
            except Exception as exc:
                logger.error(
                    f"[PreScaler] handle_spike_prediction failed for {subject}: {exc}",
                    exc_info=True,
                )

        await self._nats.subscribe_raw(
            "ppa.predictions.>", handler=handler, stream_name="PPA_PREDICTIONS"
        )

    async def handle_spike_prediction(self, event: IncidentEvent) -> None:
        """Entry point: called when a TRAFFIC_SPIKE_PREDICTED event arrives."""
        if event.signal_type != SignalType.TRAFFIC_SPIKE_PREDICTED:
            return

        self._events_received += 1
        ctx = event.context
        current_rps = float(ctx.get("current_rps", 0.0))
        predicted_rps = float(ctx.get("predicted_rps", 0.0))
        confidence = float(ctx.get("confidence", 0.0))
        endpoint = str(ctx.get("endpoint", "/unknown"))
        horizon = int(ctx.get("prediction_horizon_minutes", 10))
        db_table = ctx.get("db_table_trigger")
        namespace = event.namespace or "default"
        deployment = event.resource_name or endpoint_to_deployment(endpoint)

        if confidence < self._min_confidence:
            self._skipped_confidence += 1
            return

        increase_pct = 100.0 * (predicted_rps - current_rps) / max(current_rps, 1.0)
        if increase_pct < self._threshold_pct:
            self._skipped_threshold += 1
            return

        if self._is_in_cooldown(deployment):
            self._skipped_cooldown += 1
            return

        recommended = compute_recommended_replicas(
            predicted_rps, self._target_rps, self._max_replicas
        )
        current = await self._get_current_replicas(deployment, namespace)
        if recommended <= current:
            return

        decision = PrescaleDecision(
            decision_id=str(uuid.uuid4())[:8].upper(),
            deployment_name=deployment,
            namespace=namespace,
            endpoint=endpoint,
            current_rps=round(current_rps, 2),
            predicted_rps=round(predicted_rps, 2),
            horizon_minutes=horizon,
            current_replicas=current,
            recommended_replicas=recommended,
            confidence=round(confidence, 3),
            mode=self.mode,
            db_table_trigger=db_table,
        )
        self._tracker.record_decision(decision)
        self._all_decisions.append(decision)
        self._decisions_made += 1

        if self.mode == PrescaleMode.SHADOW:
            decision.executed = False
            decision.outcome = None
        elif self.mode == PrescaleMode.ADVISORY:
            await execute_advisory(decision, event, self._nats)
        elif self.mode == PrescaleMode.AUTONOMOUS:
            await execute_autonomous(decision, event, self._ladder, self._cooldown)

        await publish_prescale_decision(decision, event, self._nats)
        self._set_cooldown(deployment)

    async def _get_current_replicas(self, deployment: str, namespace: str) -> int:
        try:
            from kubernetes import client as k8s_client
            from kubernetes import config as k8s_config

            try:
                k8s_config.load_incluster_config()
            except Exception:
                k8s_config.load_kube_config()
            apps_v1 = k8s_client.AppsV1Api()
            dep = apps_v1.read_namespaced_deployment(deployment, namespace)
            return dep.spec.replicas or 1
        except Exception:
            return 1

    def _is_in_cooldown(self, deployment: str) -> bool:
        return time.monotonic() < self._cooldowns.get(deployment, 0.0)

    def _set_cooldown(self, deployment: str) -> None:
        self._cooldowns[deployment] = time.monotonic() + self._cooldown

    def promote(self, target_mode: PrescaleMode) -> str:
        old = self.mode
        self.mode = target_mode
        return f"Mode changed: {old.value} → {target_mode.value}"

    @property
    def stats(self) -> dict[str, Any]:
        prec = self._tracker.stats()
        smape = self._tracker.rolling_smape
        return {
            "mode": self.mode.value,
            "events_received": self._events_received,
            "decisions_made": self._decisions_made,
            "skipped_confidence": self._skipped_confidence,
            "skipped_threshold": self._skipped_threshold,
            "skipped_cooldown": self._skipped_cooldown,
            "precision": round(prec.precision, 3),
            "smape": round(smape, 2) if smape else None,
            "ready_for_advisory": self._tracker.ready_for_advisory(),
        }
