"""
NEXUS Finite State Machine (FSM)
================================
Deterministic incident lifecycle state machine.
Enforces valid transitions, logs state history to PostgreSQL/SQLite,
and publishes real-time transition events to NATS JetStream.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class IncidentState(str, Enum):
    DETECTED = "detected"
    CORRELATED = "correlated"
    DIAGNOSING = "diagnosing"
    PLANNING = "planning"
    POLICY_CHECK = "policy_check"
    APPROVAL_PENDING = "approval_pending"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    RESOLVED = "resolved"
    RETRYING = "retrying"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    REJECTED = "rejected"
    ESCALATED = "escalated"
    FAILED = "failed"


VALID_TRANSITIONS: dict[IncidentState, set[IncidentState]] = {
    IncidentState.DETECTED: {
        IncidentState.CORRELATED,
        IncidentState.DIAGNOSING,
        IncidentState.FAILED,
    },
    IncidentState.CORRELATED: {
        IncidentState.DIAGNOSING,
        IncidentState.FAILED,
    },
    IncidentState.DIAGNOSING: {
        IncidentState.PLANNING,
        IncidentState.POLICY_CHECK,
        IncidentState.FAILED,
        IncidentState.ESCALATED,
    },
    IncidentState.PLANNING: {
        IncidentState.POLICY_CHECK,
        IncidentState.FAILED,
        IncidentState.ESCALATED,
    },
    IncidentState.POLICY_CHECK: {
        IncidentState.EXECUTING,
        IncidentState.APPROVAL_PENDING,
        IncidentState.REJECTED,
        IncidentState.FAILED,
        IncidentState.ESCALATED,
    },
    IncidentState.APPROVAL_PENDING: {
        IncidentState.EXECUTING,
        IncidentState.REJECTED,
        IncidentState.FAILED,
        IncidentState.ESCALATED,
    },
    IncidentState.EXECUTING: {
        IncidentState.VERIFYING,
        IncidentState.ROLLING_BACK,
        IncidentState.FAILED,
    },
    IncidentState.VERIFYING: {
        IncidentState.RESOLVED,
        IncidentState.RETRYING,
        IncidentState.ROLLING_BACK,
        IncidentState.ESCALATED,
        IncidentState.FAILED,
    },
    IncidentState.RETRYING: {
        IncidentState.PLANNING,
        IncidentState.EXECUTING,
        IncidentState.ESCALATED,
        IncidentState.FAILED,
    },
    IncidentState.ROLLING_BACK: {
        IncidentState.ROLLED_BACK,
        IncidentState.ESCALATED,
        IncidentState.FAILED,
    },
    # Terminal states can transition to escalated if post-mortems or operators require it
    IncidentState.RESOLVED: set(),
    IncidentState.ROLLED_BACK: {IncidentState.ESCALATED},
    IncidentState.REJECTED: {IncidentState.ESCALATED},
    IncidentState.ESCALATED: set(),
    IncidentState.FAILED: {IncidentState.ESCALATED},
}


class IncidentFSM:
    """
    Finite State Machine driving an incident through its lifecycle.
    """

    def __init__(
        self,
        incident_id: str,
        current_state: IncidentState = IncidentState.DETECTED,
        db_client: Any = None,
        nats_client: Any = None,
    ) -> None:
        self.incident_id = incident_id
        self._current_state = current_state
        self.db_client = db_client
        self.nats = nats_client

    @property
    def current_state(self) -> IncidentState:
        return self._current_state

    def is_terminal(self) -> bool:
        return len(VALID_TRANSITIONS.get(self._current_state, set())) == 0

    def can_transition_to(self, target_state: IncidentState | str) -> bool:
        target = IncidentState(target_state) if isinstance(target_state, str) else target_state
        allowed = VALID_TRANSITIONS.get(self._current_state, set())
        return target in allowed

    async def transition_to(
        self,
        target_state: IncidentState | str,
        reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """
        Transition the incident to target_state.
        Validates the transition, logs to the database, and broadcasts to NATS.
        """
        target = IncidentState(target_state) if isinstance(target_state, str) else target_state

        if not self.can_transition_to(target):
            logger.error(
                "[IncidentFSM] Invalid state transition for incident %s: %s -> %s",
                self.incident_id,
                self._current_state.value,
                target.value,
            )
            return False

        from_state = self._current_state
        self._current_state = target
        now_iso = datetime.now(timezone.utc).isoformat()

        logger.info(
            "[IncidentFSM] Incident %s: %s -> %s (reason: %s)",
            self.incident_id,
            from_state.value,
            target.value,
            reason,
        )

        # 1. Update Database
        if self.db_client:
            try:
                await self.db_client.transition_state(
                    incident_id=self.incident_id,
                    to_state=target.value,
                    reason=reason,
                    metadata=metadata,
                )
            except Exception as db_exc:
                logger.error(
                    "[IncidentFSM] Failed to persist state transition to database: %s",
                    db_exc,
                    exc_info=True,
                )

        # 2. Publish to NATS JetStream
        if self.nats:
            try:
                subject = f"nexus.fsm.{self.incident_id}.transition"
                payload = {
                    "incident_id": self.incident_id,
                    "from_state": from_state.value,
                    "to_state": target.value,
                    "reason": reason,
                    "metadata": metadata or {},
                    "timestamp": now_iso,
                }
                # Support both nats_client.publish_raw and nats.publish
                call = None
                if hasattr(self.nats, "publish_raw"):
                    call = self.nats.publish_raw(subject, payload)
                elif hasattr(self.nats, "publish"):
                    import json
                    call = self.nats.publish(subject, json.dumps(payload).encode("utf-8"))
                import asyncio
                if asyncio.iscoroutine(call):
                    await call
            except Exception as nats_exc:
                logger.warning(
                    "[IncidentFSM] Failed to publish FSM transition to NATS: %s",
                    nats_exc,
                )

        return True
