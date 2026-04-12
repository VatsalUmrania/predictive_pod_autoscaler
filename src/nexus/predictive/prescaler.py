"""
NEXUS Prescaler
================
Receives TRAFFIC_SPIKE_PREDICTED events and decides whether to pre-scale
Kubernetes deployments before the traffic spike materialises.

Gated Autonomy Modes
--------------------
    SHADOW     Log the decision only. Record what WOULD have been done.
               Measure precision after the horizon window: did the spike happen?
               Exit criteria: N=20 predictions with SMAPE < 25% AND precision ≥ 0.70

    ADVISORY   Publish a PRE_SCALE_ADVISORY NATS event + emit a human-approval request
               (enqueues into HumanApprovalQueue from Phase 3).
               A human can run: nexus prescale approve <id>
               Exit criteria: Ops team has reviewed at least 10 advisory decisions.

    AUTONOMOUS Scale deployment via ActionLadder (L2 action) immediately.
               Full governance plane applies: policy, cooldown, circuit breaker.
               This mode is not entered automatically — must be manually promoted.

Precision Tracker
-----------------
Records each shadow decision and the actual RPS observed after the prediction
horizon. Computes precision as TP / (TP + FP) where:
    TP = spike was predicted AND actual-RPS ≥ spike_threshold× base-RPS
    FP = spike was predicted AND actual-RPS < spike_threshold× base-RPS

SMAPE < 25% AND precision ≥ 0.70 over the last N=20 predictions
gates graduation from SHADOW → ADVISORY.

Configuration (environment variables):
    NEXUS_PRESCALE_MODE          shadow | advisory | autonomous (default: shadow)
    NEXUS_PRESCALE_THRESHOLD_PCT Minimum % increase to warrant pre-scaling (default: 30)
    NEXUS_PRESCALE_MAX_REPLICAS  Safety cap on replica count (default: 20)
    NEXUS_PRESCALE_COOLDOWN_S    Seconds between pre-scale for same deployment (default: 300)
    NEXUS_PRESCALE_MIN_CONFIDENCE Minimum prediction confidence to act (default: 0.55)
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Deque, Dict, List, Optional, Tuple

from nexus.bus.incident_event import AgentType, IncidentEvent, Severity, SignalType
from nexus.bus.nats_client import NATSClient
from nexus.governance.action_ladder import ActionLadder
from nexus.governance.runbook import Runbook, RunbookAction, RunbookTrigger

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Enums & dataclasses
# ──────────────────────────────────────────────────────────────────────────────

class PrescaleMode(str, Enum):
    SHADOW     = "shadow"
    ADVISORY   = "advisory"
    AUTONOMOUS = "autonomous"


@dataclass
class PrescaleDecision:
    """A single pre-scale recommendation."""
    decision_id:          str
    deployment_name:      str
    namespace:            str
    endpoint:             str
    current_rps:          float
    predicted_rps:        float
    horizon_minutes:      int
    current_replicas:     int
    recommended_replicas: int
    confidence:           float
    mode:                 PrescaleMode
    db_table_trigger:     Optional[str]

    decided_at:           str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    verified_at:          Optional[str] = None
    actual_rps:           Optional[float] = None
    outcome:              Optional[str]   = None   # "correct" | "false_positive" | "pending"
    executed:             bool = False

    def mark_outcome(self, actual_rps: float, spike_threshold: float = 1.3) -> str:
        self.actual_rps  = actual_rps
        self.verified_at = datetime.now(timezone.utc).isoformat()
        expected_spike   = self.current_rps * spike_threshold
        self.outcome     = "correct" if actual_rps >= expected_spike else "false_positive"
        return self.outcome


@dataclass
class PrecisionStats:
    """Rolling precision metrics over the last N decisions."""
    true_positives:  int = 0
    false_positives: int = 0
    pending:         int = 0

    @property
    def precision(self) -> float:
        total = self.true_positives + self.false_positives
        if total == 0:
            return 0.0
        return self.true_positives / total

    @property
    def sample_count(self) -> int:
        return self.true_positives + self.false_positives

    def __str__(self) -> str:
        return (
            f"precision={self.precision:.2f} "
            f"TP={self.true_positives} FP={self.false_positives} "
            f"pending={self.pending}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Precision Tracker
# ──────────────────────────────────────────────────────────────────────────────

class PrecisionTracker:
    """
    Tracks shadow-mode prediction accuracy.

    Records each decision and the actual RPS observed after the horizon.
    Computes precision over a rolling window of N decisions.

    Args:
        window:           Rolling window size (default 20).
        spike_threshold:  Minimum ratio for a "true spike" (default 1.3 = +30%).
    """

    def __init__(self, window: int = 20, spike_threshold: float = 1.3):
        self._window     = window
        self._threshold  = spike_threshold
        self._decisions: Dict[str, PrescaleDecision] = {}
        self._smape_deque: Deque[float] = deque(maxlen=window)

    def record_decision(self, decision: PrescaleDecision) -> None:
        self._decisions[decision.decision_id] = decision

    def record_actual(self, decision_id: str, actual_rps: float) -> Optional[str]:
        """Record the observed RPS after the horizon. Returns outcome."""
        decision = self._decisions.get(decision_id)
        if not decision:
            return None
        outcome = decision.mark_outcome(actual_rps, self._threshold)

        # SMAPE
        denom = abs(actual_rps) + abs(decision.predicted_rps)
        smape = 200.0 * abs(actual_rps - decision.predicted_rps) / max(denom, 1e-6)
        self._smape_deque.append(smape)

        return outcome

    def stats(self) -> PrecisionStats:
        recent = list(self._decisions.values())[-self._window:]
        tp = sum(1 for d in recent if d.outcome == "correct")
        fp = sum(1 for d in recent if d.outcome == "false_positive")
        pending = sum(1 for d in recent if d.outcome is None)
        return PrecisionStats(true_positives=tp, false_positives=fp, pending=pending)

    @property
    def rolling_smape(self) -> Optional[float]:
        if not self._smape_deque:
            return None
        return sum(self._smape_deque) / len(self._smape_deque)

    def ready_for_advisory(self, min_samples: int = 20, min_precision: float = 0.70, max_smape: float = 25.0) -> bool:
        """Returns True if shadow-mode results justify promoting to ADVISORY."""
        st = self.stats()
        smape = self.rolling_smape
        return (
            st.sample_count >= min_samples
            and st.precision >= min_precision
            and (smape is None or smape <= max_smape)
        )


# ──────────────────────────────────────────────────────────────────────────────
# Prescaler
# ──────────────────────────────────────────────────────────────────────────────

class Prescaler:
    """
    Pre-scale advisor and executor.

    Subscribes to TRAFFIC_SPIKE_PREDICTED events and executes
    pre-scale decisions in the configured mode.

    Args:
        nats_client:            Connected NATSClient.
        action_ladder:          Phase 3 ActionLadder (used in AUTONOMOUS mode).
        mode:                   Initial operating mode (default: SHADOW).
        max_replicas:           Safety cap (default: 20).
        min_confidence:         Minimum prediction confidence to act on (default: 0.55).
        threshold_pct:          Minimum predicted RPS increase to pre-scale (default: 30.0).
        cooldown_seconds:       Per-deployment cooldown (default: 300).
        target_rps_per_replica: Used to compute replica target (default: 100).
    """

    def __init__(
        self,
        nats_client:            NATSClient,
        action_ladder:          Optional[ActionLadder] = None,
        mode:                   PrescaleMode = PrescaleMode(
            os.getenv("NEXUS_PRESCALE_MODE", "shadow")
        ),
        max_replicas:           int   = int(os.getenv("NEXUS_PRESCALE_MAX_REPLICAS", "20")),
        min_confidence:         float = float(os.getenv("NEXUS_PRESCALE_MIN_CONFIDENCE", "0.55")),
        threshold_pct:          float = float(os.getenv("NEXUS_PRESCALE_THRESHOLD_PCT", "30")),
        cooldown_seconds:       float = float(os.getenv("NEXUS_PRESCALE_COOLDOWN_S", "300")),
        target_rps_per_replica: float = 100.0,
    ):
        self._nats              = nats_client
        self._ladder            = action_ladder
        self.mode               = mode
        self._max_replicas      = max_replicas
        self._min_confidence    = min_confidence
        self._threshold_pct     = threshold_pct
        self._cooldown          = cooldown_seconds
        self._target_rps        = target_rps_per_replica

        # Precision tracking and decision log
        self._tracker           = PrecisionTracker()
        self._all_decisions:    List[PrescaleDecision] = []
        self._cooldowns:        Dict[str, float] = {}   # deployment → mono time

        # Stats
        self._events_received   = 0
        self._decisions_made    = 0
        self._skipped_cooldown  = 0
        self._skipped_confidence = 0
        self._skipped_threshold = 0

        logger.info(
            f"[Prescaler] Started — mode={self.mode.value} "
            f"threshold={self._threshold_pct}% "
            f"min_confidence={self._min_confidence}"
        )

    # ── NATS handler ──────────────────────────────────────────────────────────

    async def handle_spike_prediction(self, event: IncidentEvent) -> None:
        """
        Entry point: called when a TRAFFIC_SPIKE_PREDICTED event arrives.
        """
        if event.signal_type != SignalType.TRAFFIC_SPIKE_PREDICTED:
            return

        self._events_received += 1
        ctx = event.context

        current_rps   = float(ctx.get("current_rps", 0.0))
        predicted_rps = float(ctx.get("predicted_rps", 0.0))
        confidence    = float(ctx.get("confidence", 0.0))
        endpoint      = str(ctx.get("endpoint", "/unknown"))
        horizon       = int(ctx.get("prediction_horizon_minutes", 10))
        db_table      = ctx.get("db_table_trigger")
        namespace     = event.namespace or "default"
        deployment    = event.resource_name or self._endpoint_to_deployment(endpoint)

        # ── Gates ─────────────────────────────────────────────────────────────

        if confidence < self._min_confidence:
            self._skipped_confidence += 1
            logger.debug(
                f"[Prescaler] Skipped: confidence {confidence:.2f} < {self._min_confidence}"
            )
            return

        increase_pct = 100.0 * (predicted_rps - current_rps) / max(current_rps, 1.0)
        if increase_pct < self._threshold_pct:
            self._skipped_threshold += 1
            logger.debug(
                f"[Prescaler] Skipped: increase {increase_pct:.1f}% < {self._threshold_pct}%"
            )
            return

        if self._is_in_cooldown(deployment):
            self._skipped_cooldown += 1
            logger.debug(f"[Prescaler] Skipped: {deployment} in cooldown")
            return

        # ── Compute recommendation ────────────────────────────────────────────
        recommended_replicas = min(
            self._max_replicas,
            max(1, int(predicted_rps / max(self._target_rps, 1.0)))
        )
        # Fetch current replicas (K8s API — in autonomous mode; estimate otherwise)
        current_replicas = await self._get_current_replicas(deployment, namespace)

        if recommended_replicas <= current_replicas:
            logger.debug(
                f"[Prescaler] No action: already at {current_replicas} replicas "
                f"(recommended same)"
            )
            return

        decision = PrescaleDecision(
            decision_id          = str(uuid.uuid4())[:8].upper(),
            deployment_name      = deployment,
            namespace            = namespace,
            endpoint             = endpoint,
            current_rps          = round(current_rps, 2),
            predicted_rps        = round(predicted_rps, 2),
            horizon_minutes      = horizon,
            current_replicas     = current_replicas,
            recommended_replicas = recommended_replicas,
            confidence           = round(confidence, 3),
            mode                 = self.mode,
            db_table_trigger     = db_table,
        )

        self._tracker.record_decision(decision)
        self._all_decisions.append(decision)
        self._decisions_made += 1

        logger.info(
            f"[Prescaler] Decision {decision.decision_id} "
            f"[{self.mode.value.upper()}] "
            f"deployment={deployment} ns={namespace} "
            f"replicas: {current_replicas} → {recommended_replicas} "
            f"rps: {current_rps:.0f} → {predicted_rps:.0f} "
            f"conf={confidence:.2f} "
            f"horizon={horizon}min"
        )

        # ── Mode dispatch ─────────────────────────────────────────────────────
        if self.mode == PrescaleMode.SHADOW:
            await self._shadow_execute(decision)
        elif self.mode == PrescaleMode.ADVISORY:
            await self._advisory_execute(decision, event)
        elif self.mode == PrescaleMode.AUTONOMOUS:
            await self._autonomous_execute(decision, event)

        self._set_cooldown(deployment)

    # ── Mode implementations ──────────────────────────────────────────────────

    async def _shadow_execute(self, decision: PrescaleDecision) -> None:
        """
        SHADOW mode: log the decision, record for precision tracking.
        No K8s API calls are made.
        """
        logger.info(
            f"[Prescaler] [SHADOW] Would scale {decision.deployment_name} "
            f"{decision.current_replicas} → {decision.recommended_replicas} replicas "
            f"in {decision.namespace}  (not executed — shadow mode)"
        )
        decision.executed = False
        decision.outcome  = None   # Will be set by record_actual()

        # Log graduated precision
        stats = self._tracker.stats()
        logger.debug(f"[Prescaler] [SHADOW] Tracker: {stats}")

        if self._tracker.ready_for_advisory():
            logger.warning(
                f"[Prescaler] 🎓 Shadow-mode precision criteria MET — "
                f"promote to ADVISORY with: nexus prescale set-mode advisory\n"
                f"  Stats: {stats}"
            )

    async def _advisory_execute(self, decision: PrescaleDecision, source_event: IncidentEvent) -> None:
        """
        ADVISORY mode: publish a NATS advisory event. Human must approve.
        """
        advisory_event = IncidentEvent(
            agent         = AgentType.ORCHESTRATOR,
            signal_type   = SignalType.ANOMALY_PREDICTED,   # closest available
            severity      = Severity.WARNING,
            namespace     = decision.namespace,
            resource_name = decision.deployment_name,
            correlation_id = source_event.correlation_id or source_event.event_id,
            context = {
                "type":                  "pre_scale_advisory",
                "decision_id":           decision.decision_id,
                "deployment":            decision.deployment_name,
                "namespace":             decision.namespace,
                "current_replicas":      decision.current_replicas,
                "recommended_replicas":  decision.recommended_replicas,
                "current_rps":           decision.current_rps,
                "predicted_rps":         decision.predicted_rps,
                "horizon_minutes":       decision.horizon_minutes,
                "confidence":            decision.confidence,
                "db_table_trigger":      decision.db_table_trigger,
                "action":                "Run: nexus prescale approve " + decision.decision_id,
            },
            confidence = decision.confidence,
        )
        await self._nats.publish(advisory_event)
        logger.info(
            f"[Prescaler] [ADVISORY] Published — "
            f"decision_id={decision.decision_id} "
            f"→ Run: nexus prescale approve {decision.decision_id}"
        )

    async def _autonomous_execute(self, decision: PrescaleDecision, source_event: IncidentEvent) -> None:
        """
        AUTONOMOUS mode: scale deployment directly via ActionLadder (L2 action).
        Full governance plane applies — policy, cooldown, circuit breaker.
        """
        if self._ladder is None:
            logger.error("[Prescaler] AUTONOMOUS mode requires ActionLadder — not configured")
            return

        # Build a synthetic Runbook for the ActionLadder governance checks
        scale_action  = RunbookAction(
            type="scale_deployment",
            description=f"Pre-scale {decision.deployment_name} for predicted traffic spike",
            params={
                "namespace": decision.namespace,
                "name":      decision.deployment_name,
                "replicas":  decision.recommended_replicas,
            },
        )
        synthetic_runbook = Runbook(
            id            = f"prescale_{decision.decision_id}",
            description   = "Predictive pre-scale",
            failure_class = "resource_exhaustion",
            healing_level = 2,
            blast_radius  = "single_deployment",
            cooldown_seconds = int(self._cooldown),
            trigger       = RunbookTrigger(signal_types=["traffic_spike_predicted"]),
            actions       = [scale_action],
        )

        ladder_decision = await self._ladder.evaluate(
            runbook    = synthetic_runbook,
            action     = scale_action,
            event      = source_event,
            target     = f"{decision.namespace}/{decision.deployment_name}",
            confidence = decision.confidence,
        )

        if not ladder_decision.can_proceed:
            logger.warning(
                f"[Prescaler] [AUTONOMOUS] Blocked by governance: "
                f"{ladder_decision.denial_reason}"
            )
            decision.outcome = "governance_blocked"
            return

        # Execute K8s scale via kubernetes client
        try:
            from kubernetes import client as k8s_client, config as k8s_config
            try:
                k8s_config.load_incluster_config()
            except Exception:
                k8s_config.load_kube_config()

            apps_v1 = k8s_client.AppsV1Api()
            patch   = {"spec": {"replicas": decision.recommended_replicas}}
            apps_v1.patch_namespaced_deployment_scale(
                decision.deployment_name, decision.namespace, patch
            )
            await self._ladder.set_cooldown(synthetic_runbook, f"{decision.namespace}/{decision.deployment_name}")
            decision.executed = True
            decision.outcome  = "executed"
            logger.info(
                f"[Prescaler] [AUTONOMOUS] ✅ Scaled "
                f"{decision.namespace}/{decision.deployment_name} "
                f"→ {decision.recommended_replicas} replicas"
            )
        except Exception as exc:
            decision.outcome = "failed"
            logger.error(f"[Prescaler] [AUTONOMOUS] Scale failed: {exc}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _get_current_replicas(self, deployment: str, namespace: str) -> int:
        """Query K8s for current replica count. Returns 1 on error (safe fallback)."""
        try:
            from kubernetes import client as k8s_client, config as k8s_config
            try:
                k8s_config.load_incluster_config()
            except Exception:
                k8s_config.load_kube_config()
            apps_v1 = k8s_client.AppsV1Api()
            dep = apps_v1.read_namespaced_deployment(deployment, namespace)
            return dep.spec.replicas or 1
        except Exception:
            return 1    # Conservative fallback

    @staticmethod
    def _endpoint_to_deployment(endpoint: str) -> str:
        """Heuristic: /api/payments → payments-api."""
        parts = [p for p in endpoint.strip("/").split("/") if p]
        if parts:
            name = parts[-1].replace("_", "-")
            return f"{name}-api" if not name.endswith("-api") else name
        return "unknown"

    def _is_in_cooldown(self, deployment: str) -> bool:
        import time
        expiry = self._cooldowns.get(deployment, 0.0)
        return time.monotonic() < expiry

    def _set_cooldown(self, deployment: str) -> None:
        import time
        self._cooldowns[deployment] = time.monotonic() + self._cooldown

    # ── Mode promotion ────────────────────────────────────────────────────────

    def promote(self, target_mode: PrescaleMode) -> str:
        """Promote the prescaler to a higher autonomy mode (operator command)."""
        old = self.mode
        self.mode = target_mode
        logger.warning(
            f"[Prescaler] MODE CHANGE: {old.value} → {target_mode.value} "
            f"(precision: {self._tracker.stats()})"
        )
        return f"Mode changed: {old.value} → {target_mode.value}"

    # ── Observability ─────────────────────────────────────────────────────────

    @property
    def stats(self) -> Dict[str, Any]:
        prec  = self._tracker.stats()
        smape = self._tracker.rolling_smape
        return {
            "mode":                self.mode.value,
            "events_received":     self._events_received,
            "decisions_made":      self._decisions_made,
            "skipped_confidence":  self._skipped_confidence,
            "skipped_threshold":   self._skipped_threshold,
            "skipped_cooldown":    self._skipped_cooldown,
            "precision":           round(prec.precision, 3),
            "smape":               round(smape, 2) if smape else None,
            "ready_for_advisory":  self._tracker.ready_for_advisory(),
        }
