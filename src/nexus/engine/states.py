"""
NEXUS Engine States & Valid Transitions
=======================================
Deterministic incident lifecycle state definitions and transition table.
"""

from __future__ import annotations

from enum import Enum


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
        IncidentState.POLICY_CHECK,
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
