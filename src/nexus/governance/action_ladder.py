"""
NEXUS Action Ladder
====================
Routes healing actions through the 4-level Governance Plane.

Level taxonomy:
    L0 — detect + alert only (emit_alert, patch_annotation)
         Always allowed; zero blast radius
    L1 — no-regret actions (restart_pod, flush_coredns_cache)
         Allowed unless cooldown or governance CB open
    L2 — bounded mitigation (scale_deployment, canary halt)
         L1 conditions + blast-radius check
    L3 — significant change (rollout undo, config revert)
         L2 conditions + confidence >= 0.85 OR explicit human approval

Key safety component — GovernanceCircuitBreaker:
    Tracks consecutive post-check SLO failures across all runbooks.
    If N consecutive healings fail their post-checks, the circuit trips OPEN
    and ALL autonomous healing is blocked until manually reset.
    This prevents "healing loops" from making a degraded system worse.

HumanApprovalQueue:
    L3 actions with confidence < 0.85 are staged here.
    A human operator calls approve(approval_id) via the CLI or API.
    Once approved, the runbook executor retries the action.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from nexus.bus.incident_event import IncidentEvent
from nexus.governance.cooldown_store import CooldownStore
from nexus.governance.policy_engine import PolicyDecision, PolicyEngine
from nexus.governance.runbook import Runbook, RunbookAction

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Governance Circuit Breaker
# ──────────────────────────────────────────────────────────────────────────────

class GovernanceCircuitBreaker:
    """
    Stops autonomous healing when consecutive post-check failures indicate
    the healing system itself may be misconfigured or causing harm.

    States:
        CLOSED:    Normal — healing actions are permitted.
        OPEN:      Too many failures — autonomous healing blocked.
                   L0 (emit_alert, patch_annotation) still allowed.

    Args:
        failure_threshold:  Consecutive post-check failures to trip OPEN (default 3).
        nats_client:        Optional — if provided, emits a CIRCUIT_BREAKER_TRIPPED event.
    """

    CLOSED = "CLOSED"
    OPEN   = "OPEN"

    def __init__(self, failure_threshold: int = 3, nats_client=None):
        self._threshold   = failure_threshold
        self._failures    = 0
        self._state       = self.CLOSED
        self._nats        = nats_client
        self._tripped_at: Optional[str] = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def is_open(self) -> bool:
        return self._state == self.OPEN

    @property
    def consecutive_failures(self) -> int:
        return self._failures

    def record_post_check_success(self) -> None:
        """Call after a runbook's post-checks pass."""
        prev = self._failures
        self._failures = 0
        self._state    = self.CLOSED
        if prev > 0:
            logger.info(f"[GovernanceCB] RESET after {prev} consecutive failure(s)")

    def record_post_check_failure(self) -> None:
        """Call when a runbook's post-checks fail (healing didn't work)."""
        self._failures += 1
        if self._failures >= self._threshold:
            if self._state != self.OPEN:
                self._tripped_at = datetime.now(timezone.utc).isoformat()
                self._state = self.OPEN
                logger.critical(
                    f"[GovernanceCB] TRIPPED OPEN — {self._failures} consecutive "
                    f"post-check failures. Autonomous healing SUSPENDED."
                )

    def reset(self) -> None:
        """Manually reset the circuit breaker (human operator action)."""
        self._failures   = 0
        self._state      = self.CLOSED
        self._tripped_at = None
        logger.warning("[GovernanceCB] Manually RESET by operator")

    def status_dict(self) -> dict:
        return {
            "state":               self._state,
            "consecutive_failures": self._failures,
            "threshold":           self._threshold,
            "tripped_at":          self._tripped_at,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Human Approval Queue
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class PendingApproval:
    approval_id:   str
    runbook_id:    str
    action_type:   str
    target:        str
    incident_id:   str
    healing_level: int
    confidence:    float
    enqueued_at:   str
    context:       dict = field(default_factory=dict)


class HumanApprovalQueue:
    """
    Staging queue for L3 actions awaiting human approval.

    In Phase 3 this is in-memory.
    Phase 7 will wire this to the CLI (`nexus approve <id>`), Slack bot,
    and PagerDuty webhook so approvals can arrive from any channel.
    """

    def __init__(self):
        self._pending:  Dict[str, PendingApproval] = {}
        self._approved: Set[str] = set()
        self._rejected: Set[str] = set()

    def enqueue(
        self,
        runbook_id: str,
        action_type: str,
        target: str,
        incident_id: str,
        healing_level: int,
        confidence: float,
        context: Optional[dict] = None,
    ) -> str:
        """
        Stage an action for human approval.
        Returns an approval_id the operator passes to approve() or reject().
        """
        approval_id = str(uuid.uuid4())[:8].upper()
        self._pending[approval_id] = PendingApproval(
            approval_id   = approval_id,
            runbook_id    = runbook_id,
            action_type   = action_type,
            target        = target,
            incident_id   = incident_id,
            healing_level = healing_level,
            confidence    = confidence,
            enqueued_at   = datetime.now(timezone.utc).isoformat(),
            context       = context or {},
        )
        logger.warning(
            f"[HumanApprovalQueue] L3 action staged — approval_id={approval_id} "
            f"runbook={runbook_id} target={target} confidence={confidence:.2f}\n"
            f"  → Run: nexus approve {approval_id}  OR  nexus reject {approval_id}"
        )
        return approval_id

    def approve(self, approval_id: str) -> bool:
        """Operator approves a pending action."""
        if approval_id in self._pending:
            self._approved.add(approval_id)
            logger.info(f"[HumanApprovalQueue] APPROVED: {approval_id}")
            return True
        return False

    def reject(self, approval_id: str) -> bool:
        """Operator rejects a pending action."""
        if approval_id in self._pending:
            self._rejected.add(approval_id)
            logger.info(f"[HumanApprovalQueue] REJECTED: {approval_id}")
            return True
        return False

    def is_approved(self, approval_id: str) -> bool:
        return approval_id in self._approved

    def is_rejected(self, approval_id: str) -> bool:
        return approval_id in self._rejected

    def pending_list(self) -> List[PendingApproval]:
        return [
            p for approval_id, p in self._pending.items()
            if approval_id not in self._approved and approval_id not in self._rejected
        ]

    def clear_resolved(self) -> None:
        """Remove approved/rejected items from the pending dict."""
        resolved = self._approved | self._rejected
        for rid in resolved:
            self._pending.pop(rid, None)


# ──────────────────────────────────────────────────────────────────────────────
# Ladder Decision
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class LadderDecision:
    can_proceed:          bool
    requires_approval:    bool = False
    approval_id:          Optional[str] = None
    denial_reason:        Optional[str] = None
    policy_decision:      Optional[PolicyDecision] = None
    cooldown_remaining_s: float = 0.0


# ──────────────────────────────────────────────────────────────────────────────
# Action Ladder
# ──────────────────────────────────────────────────────────────────────────────

class ActionLadder:
    """
    Governance router for all NEXUS healing actions.

    Evaluates L0–L3 actions against:
        1. Governance circuit breaker (consecutive post-check failures)
        2. OPA policy (action type allowlist, blast radius, confidence)
        3. Cooldown store (prevent rapid re-execution)
        4. Human approval queue (L3 with confidence < 0.85)

    Args:
        policy_engine:        PolicyEngine (OPA + fallback).
        cooldown_store:       CooldownStore (Redis + memory fallback).
        approval_queue:       HumanApprovalQueue for L3 staging.
        governance_cb:        GovernanceCircuitBreaker.
        l3_confidence_gate:   Minimum confidence to auto-approve L3 (default 0.85).
    """

    def __init__(
        self,
        policy_engine: PolicyEngine,
        cooldown_store: CooldownStore,
        approval_queue: HumanApprovalQueue,
        governance_cb: GovernanceCircuitBreaker,
        l3_confidence_gate: float = 0.85,
    ):
        self._policy       = policy_engine
        self._cooldown     = cooldown_store
        self._approval     = approval_queue
        self._cb           = governance_cb
        self._l3_gate      = l3_confidence_gate

    async def evaluate(
        self,
        runbook: Runbook,
        action: RunbookAction,
        event: IncidentEvent,
        target: str,
        confidence: float = 1.0,
        human_approved: bool = False,
    ) -> LadderDecision:
        """
        Evaluate whether an action may proceed.

        Returns:
            LadderDecision(can_proceed=True)         — execute immediately
            LadderDecision(requires_approval=True)   — stage for human approval
            LadderDecision(can_proceed=False)         — blocked (reason included)
        """
        level       = runbook.healing_level
        action_type = action.type
        blast_radius = runbook.blast_radius

        # ── L0 fast path ──────────────────────────────────────────────────────
        # L0 actions (emit_alert, patch_annotation) bypass CB + cooldown checks.
        # They are always allowed — blocking alerts would defeat the purpose.
        if level == 0:
            return LadderDecision(can_proceed=True)

        # ── Governance circuit breaker ────────────────────────────────────────
        if self._cb.is_open:
            logger.warning(
                f"[ActionLadder] BLOCKED by governance CB: "
                f"{action_type} for {target} (L{level})"
            )
            return LadderDecision(
                can_proceed=False,
                denial_reason=(
                    f"governance_cb_open — {self._cb.consecutive_failures} consecutive "
                    f"post-check failures; autonomous healing suspended"
                ),
            )

        # ── Cooldown check ────────────────────────────────────────────────────
        cooldown_key = CooldownStore.make_key(runbook.id, target)
        in_cooldown  = await self._cooldown.is_in_cooldown(cooldown_key)
        remaining    = await self._cooldown.remaining_seconds(cooldown_key) if in_cooldown else 0.0

        if in_cooldown:
            logger.info(
                f"[ActionLadder] COOLDOWN: {runbook.id} on {target} "
                f"({remaining:.0f}s remaining)"
            )
            return LadderDecision(
                can_proceed=False,
                denial_reason=f"in_cooldown ({remaining:.0f}s remaining)",
                cooldown_remaining_s=remaining,
            )

        # ── OPA policy check ──────────────────────────────────────────────────
        policy = await self._policy.evaluate(
            action_type          = action_type,
            healing_level        = level,
            blast_radius         = blast_radius,
            in_cooldown          = in_cooldown,
            governance_cb_open   = self._cb.is_open,
            confidence           = confidence,
            human_approved       = human_approved,
            override_blast_radius= getattr(event, "override_blast_radius", False),
        )

        # ── L3 + confidence gate → human approval ────────────────────────────
        if policy.requires_approval and not human_approved:
            approval_id = self._approval.enqueue(
                runbook_id    = runbook.id,
                action_type   = action_type,
                target        = target,
                incident_id   = event.correlation_id or event.event_id,
                healing_level = level,
                confidence    = confidence,
                context       = {
                    "event_id":    event.event_id,
                    "signal_type": event.signal_type,
                    "namespace":   event.namespace,
                    "resource":    event.resource_name,
                },
            )
            return LadderDecision(
                can_proceed       = False,
                requires_approval = True,
                approval_id       = approval_id,
                policy_decision   = policy,
                denial_reason     = "requires_human_approval",
            )

        # ── Policy denied ─────────────────────────────────────────────────────
        if not policy.allowed:
            logger.info(
                f"[ActionLadder] POLICY DENIED: {action_type} L{level} "
                f"— {policy.deny_reasons}"
            )
            return LadderDecision(
                can_proceed     = False,
                denial_reason   = "; ".join(policy.deny_reasons),
                policy_decision = policy,
            )

        logger.info(
            f"[ActionLadder] APPROVED: {action_type} L{level} "
            f"blast={blast_radius} confidence={confidence:.2f} "
            f"policy_source={policy.source}"
        )
        return LadderDecision(can_proceed=True, policy_decision=policy)

    def record_post_check_success(self) -> None:
        """Call after successful post-checks to reset governance CB."""
        self._cb.record_post_check_success()

    def record_post_check_failure(self) -> None:
        """Call after failed post-checks — may trip governance CB."""
        self._cb.record_post_check_failure()

    async def set_cooldown(self, runbook: Runbook, target: str) -> None:
        """Set the cooldown for this runbook+target after successful execution."""
        key = CooldownStore.make_key(runbook.id, target)
        await self._cooldown.set_cooldown(key, runbook.cooldown_seconds)

    @property
    def governance_cb(self) -> GovernanceCircuitBreaker:
        return self._cb

    @property
    def approval_queue(self) -> HumanApprovalQueue:
        return self._approval
