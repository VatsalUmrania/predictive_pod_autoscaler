"""NEXUS Action Ladder.

Routes healing actions through the 4-level Governance Plane.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from nexus.governance.approval_queue import (
    HumanApprovalQueue,
    PendingApproval,
)
from nexus.governance.circuit_breaker import GovernanceCircuitBreaker
from nexus.governance.cooldown_store import CooldownStore
from nexus.governance.policy_engine import PolicyDecision, PolicyEngine

__all__ = [
    "ActionLadder",
    "LadderDecision",
    "HumanApprovalQueue",
    "PendingApproval",
]


logger = logging.getLogger(__name__)


@dataclass
class LadderDecision:
    can_proceed: bool
    requires_approval: bool = False
    approval_id: str | None = None
    denial_reason: str | None = None
    policy_decision: PolicyDecision | None = None
    cooldown_remaining_s: float = 0.0


class ActionLadder:
    """Governance router for all NEXUS healing actions."""

    def __init__(
        self,
        policy_engine: PolicyEngine,
        cooldown_store: CooldownStore,
        approval_queue: HumanApprovalQueue,
        governance_cb: GovernanceCircuitBreaker,
        l3_confidence_gate: float = 0.85,
    ):
        self._policy = policy_engine
        self._cooldown = cooldown_store
        self._approval = approval_queue
        self._cb = governance_cb
        self._l3_gate = l3_confidence_gate

    async def evaluate(
        self,
        runbook: Any = None,
        action: Any = None,
        event: Any = None,
        target: str = "",
        confidence: float = 1.0,
        human_approved: bool = False,
        override_blast_radius: bool = False,
        action_type: str | None = None,
        healing_level: int | None = None,
        blast_radius: str | None = None,
    ) -> LadderDecision:
        level = (
            healing_level
            if healing_level is not None
            else (getattr(runbook, "healing_level", 1) if runbook else 1)
        )
        action_type_val: str = str(
            action_type
            or (getattr(action, "type", str(action)) if action else "unknown")
        )
        blast_radius_val: str = str(
            blast_radius
            or (
                getattr(runbook, "blast_radius", "single_pod")
                if runbook
                else "single_pod"
            )
        )
        action_id_val: str = str(
            getattr(runbook, "id", action_type_val) if runbook else action_type_val
        )

        # L0 actions (emit_alert, patch_annotation) always allowed
        if level == 0:
            return LadderDecision(can_proceed=True)

        # Governance circuit breaker
        if self._cb.is_open:
            logger.warning(
                f"[ActionLadder] BLOCKED by governance CB: {action_type_val} for {target} (L{level})"
            )
            return LadderDecision(
                can_proceed=False,
                denial_reason=(
                    f"governance_cb_open — {self._cb.consecutive_failures} consecutive "
                    f"post-check failures; autonomous healing suspended"
                ),
            )

        # Cooldown check
        cooldown_key = CooldownStore.make_key(action_id_val, target)
        in_cooldown = await self._cooldown.is_in_cooldown(cooldown_key)
        remaining = (
            await self._cooldown.remaining_seconds(cooldown_key) if in_cooldown else 0.0
        )

        if in_cooldown:
            logger.info(
                f"[ActionLadder] COOLDOWN: {action_id_val} on {target} ({remaining:.0f}s remaining)"
            )
            return LadderDecision(
                can_proceed=False,
                denial_reason=f"in_cooldown ({remaining:.0f}s remaining)",
                cooldown_remaining_s=remaining,
            )

        # OPA policy check
        policy = await self._policy.evaluate(
            action_type=action_type_val,
            healing_level=level,
            blast_radius=blast_radius_val,
            in_cooldown=in_cooldown,
            governance_cb_open=self._cb.is_open,
            confidence=confidence,
            human_approved=human_approved,
            override_blast_radius=override_blast_radius
            or getattr(event, "override_blast_radius", False),
        )

        # Universal Human Approval requirement
        if (policy.requires_approval or not human_approved) and action_type_val not in (
            "emit_alert",
            "patch_annotation",
        ):
            inc_id: str = str(
                getattr(event, "correlation_id", None)
                or getattr(event, "event_id", "incident-unknown")
            )
            evt_id = getattr(event, "event_id", inc_id)
            approval_id = self._approval.enqueue(
                runbook_id=action_id_val,
                action_type=action_type_val,
                target=target,
                incident_id=inc_id,
                healing_level=level,
                confidence=confidence,
                context={
                    "event_id": evt_id,
                    "signal_type": getattr(event, "signal_type", "unknown"),
                    "namespace": getattr(event, "namespace", "default"),
                    "resource": getattr(event, "resource_name", target),
                    "action": (
                        action.model_dump()
                        if hasattr(action, "model_dump")
                        else {
                            "type": action_type_val,
                            "params": getattr(action, "params", {}),
                        }
                    ),
                    "event": (
                        event.model_dump() if hasattr(event, "model_dump") else None
                    ),
                    "blast_radius": blast_radius,
                },
            )
            return LadderDecision(
                can_proceed=False,
                requires_approval=True,
                approval_id=approval_id,
                policy_decision=policy,
                denial_reason="requires_human_approval",
            )

        if not policy.allowed:
            logger.info(
                f"[ActionLadder] POLICY DENIED: {action_type} L{level} — {policy.deny_reasons}"
            )
            return LadderDecision(
                can_proceed=False,
                denial_reason="; ".join(policy.deny_reasons),
                policy_decision=policy,
            )

        logger.info(
            f"[ActionLadder] APPROVED: {action_type} L{level} blast={blast_radius} confidence={confidence:.2f}"
        )
        return LadderDecision(can_proceed=True, policy_decision=policy)

    def record_post_check_success(self) -> None:
        self._cb.record_post_check_success()

    def record_post_check_failure(self) -> None:
        self._cb.record_post_check_failure()

    async def set_cooldown(
        self, runbook_or_id: Any, target: str, cooldown_seconds: int = 300
    ) -> None:
        key_id = getattr(runbook_or_id, "id", str(runbook_or_id))
        secs = getattr(runbook_or_id, "cooldown_seconds", cooldown_seconds)
        key = CooldownStore.make_key(key_id, target)
        await self._cooldown.set_cooldown(key, secs)

    @property
    def governance_cb(self) -> GovernanceCircuitBreaker:
        return self._cb

    @property
    def cooldown_store(self) -> CooldownStore:
        return self._cooldown

    @property
    def approval_queue(self) -> HumanApprovalQueue:
        return self._approval
