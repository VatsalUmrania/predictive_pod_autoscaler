"""
NEXUS RCA Validator Facade.
===========================
Backward-compatible re-exports from nexus.reasoning.validator.
"""

from __future__ import annotations

import copy
from typing import Any

from nexus.reasoning.validator import (
    _ACTION_EVIDENCE,
    _CASCADING_WITHOUT_DIVERSITY_PENALTY,
    _DISAGREEMENT_PENALTY,
    _EVIDENCE_GAP_PENALTY,
    _EVIDENCE_REQUIREMENTS,
    _VERDICT_PASS,
    EvidenceRequirement,
    RCAValidator,
    ValidationVerdict,
    _normalize_signals,
)


def downgrade_rca(rca: Any, reason: str, cluster: Any = None, **kwargs: Any) -> Any:
    d = copy.deepcopy(rca)
    d.failure_class = "unknown"
    d.healing_level = 0
    d.runbook_id = None
    d.suggested_action = None
    d.reasoning = f"VALIDATION BLOCKED: {reason}"
    return d


__all__ = [
    "EvidenceRequirement",
    "RCAValidator",
    "ValidationVerdict",
    "downgrade_rca",
    "_ACTION_EVIDENCE",
    "_CASCADING_WITHOUT_DIVERSITY_PENALTY",
    "_DISAGREEMENT_PENALTY",
    "_EVIDENCE_GAP_PENALTY",
    "_EVIDENCE_REQUIREMENTS",
    "_VERDICT_PASS",
    "_normalize_signals",
]
