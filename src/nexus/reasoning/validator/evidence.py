"""
Evidence gates for failure classes, suggested actions, and agent diversity.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nexus.graph.rca import RCAResult
from nexus.reasoning.validator.models import (
    _ACTION_EVIDENCE,
    _CASCADING_WITHOUT_DIVERSITY_PENALTY,
    _EVIDENCE_REQUIREMENTS,
    ValidationVerdict,
    _normalize_signals,
)

if TYPE_CHECKING:
    from nexus.reasoning.incident_cluster import IncidentCluster


def check_evidence_gate(
    cluster: IncidentCluster,
    rca_result: RCAResult,
    evidence_penalty: float,
    block_zero_evidence: bool,
) -> ValidationVerdict:
    """Assert that cluster.signal_types contains minimum signals for failure_class."""
    req = _EVIDENCE_REQUIREMENTS.get(rca_result.failure_class)
    if req is None or (not req.must_have_any and not req.must_also_have):
        return ValidationVerdict(
            passed=True,
            block_reason=None,
            confidence_delta=0.0,
            consistency_note=f"no evidence requirements defined for {rca_result.failure_class}",
        )

    sigs = _normalize_signals(cluster.signal_types)
    gaps: list[str] = []

    if req.must_have_any and not (req.must_have_any & sigs):
        gaps.extend(sorted(req.must_have_any))
        if block_zero_evidence:
            return ValidationVerdict(
                passed=False,
                block_reason=(
                    f"failure_class={rca_result.failure_class!r} requires at least one of "
                    f"{sorted(req.must_have_any)} but none present in cluster signals"
                ),
                confidence_delta=-evidence_penalty,
                consistency_note=(
                    f"BLOCKED: {rca_result.failure_class} has no primary evidence signals in cluster"
                ),
                evidence_gaps=gaps,
            )
        return ValidationVerdict(
            passed=True,
            block_reason=None,
            confidence_delta=-evidence_penalty,
            consistency_note=(
                f"{rca_result.failure_class} claimed but required signals "
                f"{sorted(req.must_have_any)} missing — penalising"
            ),
            evidence_gaps=gaps,
        )

    if req.must_also_have and not (req.must_also_have & sigs):
        missing = sorted(req.must_also_have - sigs)
        gaps.extend(missing)
        return ValidationVerdict(
            passed=True,
            block_reason=None,
            confidence_delta=-evidence_penalty * 0.5,
            consistency_note=(
                f"{rca_result.failure_class} claimed but corroborating signals "
                f"{missing} absent — partial penalty"
            ),
            evidence_gaps=gaps,
        )

    return ValidationVerdict(
        passed=True,
        block_reason=None,
        confidence_delta=0.0,
        consistency_note=f"evidence gate passed for {rca_result.failure_class}",
    )


def check_action_evidence_gate(
    cluster: IncidentCluster,
    rca_result: RCAResult,
    evidence_penalty: float,
    block_zero_evidence: bool,
) -> ValidationVerdict:
    """Assert that cluster.signal_types supports the suggested_action."""
    action = rca_result.suggested_action
    if not action:
        return ValidationVerdict(
            passed=True,
            block_reason=None,
            confidence_delta=0.0,
            consistency_note="no suggested_action — action evidence gate skipped",
        )

    required = _ACTION_EVIDENCE.get(action, frozenset())
    if not required:
        return ValidationVerdict(
            passed=True,
            block_reason=None,
            confidence_delta=0.0,
            consistency_note=f"no signal requirement for action {action!r}",
        )

    sigs = _normalize_signals(cluster.signal_types)
    if required & sigs:
        return ValidationVerdict(
            passed=True,
            block_reason=None,
            confidence_delta=0.0,
            consistency_note=(
                f"action {action!r} evidence gate passed (signals: {sorted(required & sigs)})"
            ),
        )

    if block_zero_evidence:
        return ValidationVerdict(
            passed=False,
            block_reason=(
                f"suggested_action={action!r} requires at least one of "
                f"{sorted(required)} but none present in cluster signals"
            ),
            confidence_delta=-evidence_penalty,
            consistency_note=f"BLOCKED: {action!r} lacks pod-failure evidence",
            evidence_gaps=sorted(required),
        )

    return ValidationVerdict(
        passed=True,
        block_reason=None,
        confidence_delta=-evidence_penalty,
        consistency_note=(
            f"action {action!r} requires {sorted(required)} but none present — penalising"
        ),
        evidence_gaps=sorted(required),
    )


def check_cascading_diversity(
    cluster: IncidentCluster,
    rca_result: RCAResult,
    block_cascading: bool,
) -> ValidationVerdict:
    """Check agent diversity for cascading_failure."""
    if rca_result.failure_class != "cascading_failure":
        return ValidationVerdict(
            passed=True,
            block_reason=None,
            confidence_delta=0.0,
            consistency_note="not cascading_failure — diversity check skipped",
        )

    n_agents = len(cluster.agent_types)
    if n_agents >= 2:
        return ValidationVerdict(
            passed=True,
            block_reason=None,
            confidence_delta=0.0,
            consistency_note=f"cascading_failure has agent diversity ({n_agents} agents) — check passed",
        )

    if block_cascading:
        return ValidationVerdict(
            passed=False,
            block_reason=(
                f"cascading_failure claimed but only {n_agents} agent(s) "
                "contributed signals — insufficient diversity for cascading diagnosis"
            ),
            confidence_delta=-_CASCADING_WITHOUT_DIVERSITY_PENALTY,
            consistency_note="BLOCKED: cascading_failure without multi-agent evidence",
        )

    return ValidationVerdict(
        passed=True,
        block_reason=None,
        confidence_delta=-_CASCADING_WITHOUT_DIVERSITY_PENALTY,
        consistency_note="cascading_failure from single agent — penalising (need ≥2 independent agents)",
    )
