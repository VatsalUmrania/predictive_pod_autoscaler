"""
NEXUS RCA Validator Facade.
===========================
Backward-compatible re-exports from nexus.reasoning.validator.
"""

from nexus.graph.rca import deterministic_baseline_rca as _rule_based_rca
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
    downgrade_rca,
)

__all__ = [
    "EvidenceRequirement",
    "RCAValidator",
    "ValidationVerdict",
    "_ACTION_EVIDENCE",
    "_CASCADING_WITHOUT_DIVERSITY_PENALTY",
    "_DISAGREEMENT_PENALTY",
    "_EVIDENCE_GAP_PENALTY",
    "_EVIDENCE_REQUIREMENTS",
    "_VERDICT_PASS",
    "_normalize_signals",
    "_rule_based_rca",
    "downgrade_rca",
]
