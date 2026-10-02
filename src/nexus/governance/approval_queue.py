"""Human Approval Queue for L3 and high-risk healing actions."""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _json_safe(obj: Any) -> Any:
    """Recursively convert datetime objects to ISO strings for JSON serialization."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_json_safe(v) for v in obj)
    return obj


@dataclass
class PendingApproval:
    approval_id: str
    runbook_id: str
    action_type: str
    target: str
    incident_id: str
    healing_level: int
    confidence: float
    enqueued_at: str
    context: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "approval_id": self.approval_id,
            "runbook_id": self.runbook_id,
            "action_type": self.action_type,
            "target": self.target,
            "incident_id": self.incident_id,
            "healing_level": self.healing_level,
            "confidence": round(self.confidence, 3),
            "enqueued_at": self.enqueued_at,
            "context": self.context,
        }


class HumanApprovalQueue:
    """Staging queue for L3 actions awaiting human approval."""

    def __init__(self, nats_client: Any = None):
        self._pending: dict[str, PendingApproval] = {}
        self._approved: set[str] = set()
        self._rejected: set[str] = set()
        self._nats = nats_client

    def enqueue(
        self,
        runbook_id: str,
        action_type: str,
        target: str,
        incident_id: str,
        healing_level: int,
        confidence: float,
        context: dict | None = None,
    ) -> str:
        for existing_id, existing in list(self._pending.items()):
            if existing_id in self._approved or existing_id in self._rejected:
                continue
            if existing.target == target or (incident_id and existing.incident_id == incident_id):
                existing.confidence = max(existing.confidence, confidence)
                if context:
                    existing.context.update(context)
                logger.info(
                    f"[HumanApprovalQueue] Target '{target}' (incident={incident_id}) already has pending approval "
                    f"id={existing_id} (runbook={existing.runbook_id}) — suppressing duplicate"
                )
                return existing_id

        u = uuid.uuid4().hex
        if not any(c.isalpha() for c in u[:8]):
            u = "A" + u[1:]
        approval_id = u[:8].upper()
        self._pending[approval_id] = PendingApproval(
            approval_id=approval_id,
            runbook_id=runbook_id,
            action_type=action_type,
            target=target,
            incident_id=incident_id,
            healing_level=healing_level,
            confidence=confidence,
            enqueued_at=datetime.now(timezone.utc).isoformat(),
            context=context or {},
        )
        logger.warning(
            f"[HumanApprovalQueue] L3 action staged — approval_id={approval_id} "
            f"runbook={runbook_id} target={target} confidence={confidence:.2f}\n"
            f"  → Run: nexus approve {approval_id}  OR  nexus reject {approval_id}"
        )

        if self._nats:
            import asyncio

            try:
                asyncio.get_running_loop().create_task(
                    self._nats.publish_raw(
                        "nexus.approvals.required",
                        _json_safe({
                            "approval_id": approval_id,
                            "runbook_id": runbook_id,
                            "action_type": action_type,
                            "target": target,
                            "incident_id": incident_id,
                            "healing_level": healing_level,
                            "confidence": confidence,
                            "app": (context or {}).get("namespace", "unknown"),
                            "context": context or {},
                        }),
                    )
                )
            except RuntimeError:
                pass

        return approval_id

    def get_pending_by_target(self, target: str) -> PendingApproval | None:
        for p in self.pending_list():
            if p.target == target:
                return p
        return None

    def approve(self, approval_id: str) -> bool:
        if approval_id in self._approved or approval_id in self._rejected:
            return False
        pending = self._pending.pop(approval_id, None)
        if pending is not None:
            self._approved.add(approval_id)
            logger.info(f"[HumanApprovalQueue] APPROVED: {approval_id}")
            return True
        return False

    def get(self, approval_id: str) -> PendingApproval | None:
        return self._pending.get(approval_id)

    def reject(self, approval_id: str) -> bool:
        if approval_id in self._approved or approval_id in self._rejected:
            return False
        pending = self._pending.pop(approval_id, None)
        if pending is not None:
            self._rejected.add(approval_id)
            logger.info(f"[HumanApprovalQueue] REJECTED: {approval_id}")
            return True
        return False

    def is_approved(self, approval_id: str) -> bool:
        return approval_id in self._approved

    def is_rejected(self, approval_id: str) -> bool:
        return approval_id in self._rejected

    def pending_list(self) -> list[PendingApproval]:
        return [
            p
            for approval_id, p in self._pending.items()
            if approval_id not in self._approved and approval_id not in self._rejected
        ]

    def clear_resolved(self) -> None:
        resolved = self._approved | self._rejected
        for rid in resolved:
            self._pending.pop(rid, None)
