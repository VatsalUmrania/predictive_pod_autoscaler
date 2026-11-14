"""
RCAValidator coordinator.
"""

from __future__ import annotations

import logging

from nexus.graph.rca import RCAResult
from nexus.reasoning.incident_cluster import IncidentCluster
from nexus.reasoning.validator.consistency import check_consistency
from nexus.reasoning.validator.evidence import (
    check_action_evidence_gate,
    check_cascading_diversity,
    check_evidence_gate,
)
from nexus.reasoning.validator.models import (
    _DISAGREEMENT_PENALTY,
    _EVIDENCE_GAP_PENALTY,
    _VERDICT_PASS,
    ValidationVerdict,
)

logger = logging.getLogger(__name__)


class RCAValidator:
    """
    Validates an LLM-produced RCAResult before it enters the confidence
    scoring and remediation pipeline.
    """

    def __init__(
        self,
        disagreement_penalty: float = _DISAGREEMENT_PENALTY,
        evidence_gap_penalty: float = _EVIDENCE_GAP_PENALTY,
        block_on_zero_evidence: bool = True,
        block_on_cascading_without_diversity: bool = True,
    ) -> None:
        self._disagree_penalty = disagreement_penalty
        self._evidence_penalty = evidence_gap_penalty
        self._block_zero_evidence = block_on_zero_evidence
        self._block_cascading = block_on_cascading_without_diversity

    def _consistency_check(
        self, cluster: IncidentCluster, rca_result: RCAResult
    ) -> ValidationVerdict:
        return check_consistency(cluster, rca_result, self._disagree_penalty)

    def _evidence_gate(
        self, cluster: IncidentCluster, rca_result: RCAResult
    ) -> ValidationVerdict:
        return check_evidence_gate(
            cluster, rca_result, self._evidence_penalty, self._block_zero_evidence
        )

    def _action_evidence_gate(
        self, cluster: IncidentCluster, rca_result: RCAResult
    ) -> ValidationVerdict:
        return check_action_evidence_gate(
            cluster, rca_result, self._evidence_penalty, self._block_zero_evidence
        )

    def _cascading_diversity_check(
        self, cluster: IncidentCluster, rca_result: RCAResult
    ) -> ValidationVerdict:
        return check_cascading_diversity(cluster, rca_result, self._block_cascading)

    def validate(
        self,
        cluster: IncidentCluster,
        rca_result: RCAResult,
    ) -> ValidationVerdict:
        """Run all validation checks and return a consolidated ValidationVerdict."""
        if rca_result.source == "rule_based":
            return _VERDICT_PASS

        rule_verdict = self._consistency_check(cluster, rca_result)
        evidence_verdict = self._evidence_gate(cluster, rca_result)
        action_verdict = self._action_evidence_gate(cluster, rca_result)
        cascade_verdict = self._cascading_diversity_check(cluster, rca_result)

        all_verdicts = [rule_verdict, evidence_verdict, action_verdict, cascade_verdict]

        block_reason: str | None = None
        total_penalty: float = 0.0
        notes: list[str] = []
        gaps: list[str] = []

        for v in all_verdicts:
            if v.block_reason:
                block_reason = v.block_reason
            total_penalty += abs(v.confidence_delta)
            if v.consistency_note:
                notes.append(v.consistency_note)
            gaps.extend(v.evidence_gaps)

        total_penalty = min(total_penalty, 0.40)
        passed = block_reason is None
        note = "; ".join(notes) if notes else "all checks passed"

        verdict = ValidationVerdict(
            passed=passed,
            block_reason=block_reason,
            confidence_delta=-total_penalty,
            consistency_note=note,
            evidence_gaps=list(set(gaps)),
        )

        if not passed:
            logger.warning(
                f"[RCAValidator] BLOCKED — {block_reason} "
                f"(cluster={cluster.cluster_id}, llm_class={rca_result.failure_class})"
            )
        elif total_penalty > 0:
            logger.info(
                f"[RCAValidator] Penalty −{total_penalty:.2f} applied — {note} "
                f"(cluster={cluster.cluster_id}, llm_class={rca_result.failure_class})"
            )
        else:
            logger.debug(
                f"[RCAValidator] PASS — no issues (cluster={cluster.cluster_id})"
            )

        return verdict
