"""
NEXUS Database Models and Transactional Incident Lifecycle States.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class IncidentStatus(str, Enum):
    RECEIVED = "RECEIVED"
    CORRELATED = "CORRELATED"
    INVESTIGATING = "INVESTIGATING"
    PLANNED = "PLANNED"
    VALIDATING = "VALIDATING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    VERIFYING = "VERIFYING"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    ROLLBACK_REQUIRED = "ROLLBACK_REQUIRED"
    ROLLED_BACK = "ROLLED_BACK"
    EXPIRED = "EXPIRED"
    RESOLVED = "RESOLVED"

    # Backward-compatibility aliases
    DETECTED = "RECEIVED"
    DIAGNOSING = "INVESTIGATING"
    PLANNING = "PLANNED"
    POLICY_CHECK = "VALIDATING"
    APPROVAL_PENDING = "AWAITING_APPROVAL"
    RETRYING = "PLANNED"
    ROLLING_BACK = "ROLLBACK_REQUIRED"
    FAILED = "EXECUTION_FAILED"
    ESCALATED = "EXECUTION_FAILED"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, IncidentState):
            return self is IncidentStatus(other.value)
        if isinstance(other, str):
            res = IncidentStatus._missing_(other)
            if res is not None:
                return self is res
        return super().__eq__(other)

    def __hash__(self) -> int:
        return hash(self.value)

    @classmethod
    def _missing_(cls, value: object) -> IncidentStatus | None:
        if isinstance(value, str):
            val_upper = value.upper()
            for member in cls:
                if member.value == val_upper:
                    return member
            legacy_map = {
                "DETECTED": cls.RECEIVED,
                "DIAGNOSING": cls.INVESTIGATING,
                "PLANNING": cls.PLANNED,
                "POLICY_CHECK": cls.VALIDATING,
                "APPROVAL_PENDING": cls.AWAITING_APPROVAL,
                "RETRYING": cls.PLANNED,
                "ROLLING_BACK": cls.ROLLBACK_REQUIRED,
                "FAILED": cls.EXECUTION_FAILED,
                "ESCALATED": cls.EXECUTION_FAILED,
                "APPROVED": cls.APPROVED,
                "REJECTED": cls.REJECTED,
                "EXECUTING": cls.EXECUTING,
                "VERIFYING": cls.VERIFYING,
                "RESOLVED": cls.RESOLVED,
                "ROLLED_BACK": cls.ROLLED_BACK,
            }
            if val_upper in legacy_map:
                return legacy_map[val_upper]
        return None


class IncidentState(str, Enum):
    DETECTED = "detected"
    CORRELATED = "correlated"
    DIAGNOSING = "diagnosing"
    PLANNING = "planning"
    POLICY_CHECK = "policy_check"
    APPROVAL_PENDING = "approval_pending"
    APPROVED = "approved"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    RESOLVED = "resolved"
    RETRYING = "retrying"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    REJECTED = "rejected"
    ESCALATED = "escalated"
    FAILED = "failed"

    # Uppercase aliases mapping to lowercase members
    RECEIVED = "detected"
    INVESTIGATING = "diagnosing"
    PLANNED = "planning"
    VALIDATING = "policy_check"
    AWAITING_APPROVAL = "approval_pending"
    EXECUTION_FAILED = "failed"
    VALIDATION_FAILED = "failed"
    ROLLBACK_REQUIRED = "rolling_back"
    EXPIRED = "failed"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, IncidentStatus):
            return self is IncidentState(other.value)
        if isinstance(other, str):
            res = IncidentState._missing_(other)
            if res is not None:
                return self is res
        return super().__eq__(other)

    def __hash__(self) -> int:
        return hash(self.value)

    @classmethod
    def _missing_(cls, value: object) -> IncidentState | None:
        if isinstance(value, str):
            val_upper = value.upper()
            mapping = {
                "RECEIVED": cls.DETECTED,
                "INVESTIGATING": cls.DIAGNOSING,
                "PLANNED": cls.PLANNING,
                "VALIDATING": cls.POLICY_CHECK,
                "AWAITING_APPROVAL": cls.APPROVAL_PENDING,
                "APPROVED": cls.APPROVED,
                "REJECTED": cls.REJECTED,
                "EXECUTING": cls.EXECUTING,
                "EXECUTION_FAILED": cls.FAILED,
                "VERIFYING": cls.VERIFYING,
                "VALIDATION_FAILED": cls.FAILED,
                "ROLLBACK_REQUIRED": cls.ROLLING_BACK,
                "ROLLED_BACK": cls.ROLLED_BACK,
                "EXPIRED": cls.FAILED,
                "RESOLVED": cls.RESOLVED,
                "DETECTED": cls.DETECTED,
                "CORRELATED": cls.CORRELATED,
                "DIAGNOSING": cls.DIAGNOSING,
                "PLANNING": cls.PLANNING,
                "POLICY_CHECK": cls.POLICY_CHECK,
                "APPROVAL_PENDING": cls.APPROVAL_PENDING,
                "RETRYING": cls.RETRYING,
                "ROLLING_BACK": cls.ROLLING_BACK,
                "ESCALATED": cls.ESCALATED,
                "FAILED": cls.FAILED,
            }
            if val_upper in mapping:
                return mapping[val_upper]
        return None


VALID_INCIDENT_TRANSITIONS: dict[IncidentStatus, set[IncidentStatus]] = {
    IncidentStatus.RECEIVED: {
        IncidentStatus.CORRELATED,
        IncidentStatus.INVESTIGATING,
        IncidentStatus.EXECUTION_FAILED,
    },
    IncidentStatus.CORRELATED: {
        IncidentStatus.INVESTIGATING,
        IncidentStatus.EXECUTION_FAILED,
    },
    IncidentStatus.INVESTIGATING: {
        IncidentStatus.PLANNED,
        IncidentStatus.VALIDATING,
        IncidentStatus.EXECUTION_FAILED,
    },
    IncidentStatus.PLANNED: {
        IncidentStatus.VALIDATING,
        IncidentStatus.AWAITING_APPROVAL,
        IncidentStatus.APPROVED,
        IncidentStatus.EXECUTION_FAILED,
    },
    IncidentStatus.VALIDATING: {
        IncidentStatus.AWAITING_APPROVAL,
        IncidentStatus.APPROVED,
        IncidentStatus.EXECUTING,
        IncidentStatus.VALIDATION_FAILED,
        IncidentStatus.EXECUTION_FAILED,
    },
    IncidentStatus.AWAITING_APPROVAL: {
        IncidentStatus.APPROVED,
        IncidentStatus.REJECTED,
        IncidentStatus.EXPIRED,
        IncidentStatus.EXECUTION_FAILED,
    },
    IncidentStatus.APPROVED: {
        IncidentStatus.EXECUTING,
        IncidentStatus.EXECUTION_FAILED,
    },
    IncidentStatus.EXECUTING: {
        IncidentStatus.VERIFYING,
        IncidentStatus.ROLLBACK_REQUIRED,
        IncidentStatus.EXECUTION_FAILED,
    },
    IncidentStatus.VERIFYING: {
        IncidentStatus.RESOLVED,
        IncidentStatus.PLANNED,
        IncidentStatus.ROLLBACK_REQUIRED,
        IncidentStatus.EXECUTION_FAILED,
    },
    IncidentStatus.ROLLBACK_REQUIRED: {
        IncidentStatus.ROLLED_BACK,
        IncidentStatus.EXECUTION_FAILED,
    },
    IncidentStatus.ROLLED_BACK: {IncidentStatus.EXECUTION_FAILED},
    IncidentStatus.RESOLVED: set(),
    IncidentStatus.REJECTED: set(),
    IncidentStatus.EXPIRED: set(),
    IncidentStatus.VALIDATION_FAILED: set(),
    IncidentStatus.EXECUTION_FAILED: set(),
}

VALID_TRANSITIONS = VALID_INCIDENT_TRANSITIONS


class IncidentFSM:
    """Finite State Machine driving an incident through its transactional lifecycle."""

    def __init__(
        self,
        incident_id: str,
        current_state: IncidentStatus | str = IncidentStatus.RECEIVED,
        db_client: Any = None,
        nats_client: Any = None,
    ) -> None:
        self.incident_id = incident_id
        self._current_state = (
            IncidentStatus(current_state)
            if isinstance(current_state, str)
            else current_state
        )
        self.db_client = db_client
        self.nats = nats_client

    @property
    def current_state(self) -> IncidentStatus:
        return self._current_state

    def is_terminal(self) -> bool:
        return len(VALID_INCIDENT_TRANSITIONS.get(self._current_state, set())) == 0

    def can_transition_to(self, target_state: IncidentStatus | str) -> bool:
        target = (
            IncidentStatus(target_state)
            if isinstance(target_state, str)
            else target_state
        )
        allowed = VALID_INCIDENT_TRANSITIONS.get(self._current_state, set())
        return target in allowed

    async def transition_to(
        self,
        target_state: IncidentStatus | str,
        reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        target = (
            IncidentStatus(target_state)
            if isinstance(target_state, str)
            else target_state
        )

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
                if hasattr(self.db_client, "transition_status"):
                    await self.db_client.transition_status(
                        incident_id=self.incident_id,
                        to_status=target.value,
                        reason=reason,
                    )
                if hasattr(self.db_client, "transition_state"):
                    await self.db_client.transition_state(
                        incident_id=self.incident_id,
                        to_state=target.value.lower(),
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
                call = None
                if hasattr(self.nats, "publish_raw"):
                    call = self.nats.publish_raw(subject, payload)
                elif hasattr(self.nats, "publish"):
                    import json

                    call = self.nats.publish(
                        subject, json.dumps(payload).encode("utf-8")
                    )
                if asyncio.iscoroutine(call):
                    await call
            except Exception as nats_exc:
                logger.warning(
                    "[IncidentFSM] Failed to publish FSM transition to NATS: %s",
                    nats_exc,
                )

        return True


__all__ = [
    "IncidentStatus",
    "IncidentState",
    "VALID_INCIDENT_TRANSITIONS",
    "VALID_TRANSITIONS",
    "IncidentFSM",
]
