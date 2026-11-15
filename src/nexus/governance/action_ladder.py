"""Backward compatibility re-exports for governance approval queue and circuit breaker."""

from __future__ import annotations

from nexus.governance.approval_queue import HumanApprovalQueue, PendingApproval
from nexus.governance.circuit_breaker import GovernanceCircuitBreaker

__all__ = ["HumanApprovalQueue", "PendingApproval", "GovernanceCircuitBreaker"]
