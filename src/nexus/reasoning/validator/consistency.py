"""
Consistency check: cross-validates LLM against deterministic rule engine.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nexus.graph.rca import RCAResult, deterministic_baseline_rca
from nexus.reasoning.validator.models import ValidationVerdict

if TYPE_CHECKING:
    from nexus.reasoning.incident_cluster import IncidentCluster

_rule_based_rca = deterministic_baseline_rca


def check_consistency(
    cluster: IncidentCluster,
    rca_result: RCAResult,
    disagree_penalty: float,
) -> ValidationVerdict:
    """Run _rule_based_rca on the same cluster and compare failure_class."""
    rule_rca = _rule_based_rca(cluster)
    llm_class = rca_result.failure_class
    rule_class = rule_rca.failure_class

    if llm_class == "unknown":
        return ValidationVerdict(
            passed=True,
            block_reason=None,
            confidence_delta=0.0,
            consistency_note="LLM classified unknown — conservative direction, no penalty",
        )

    if llm_class == rule_class:
        return ValidationVerdict(
            passed=True,
            block_reason=None,
            confidence_delta=0.0,
            consistency_note=f"LLM and rule engine agree: {llm_class}",
        )

    if rule_class == "unknown":
        return ValidationVerdict(
            passed=True,
            block_reason=None,
            confidence_delta=-disagree_penalty * 0.5,
            consistency_note=(
                f"LLM classified {llm_class} but rule engine returned unknown "
                "(insufficient signals for deterministic classification) — applying half-penalty"
            ),
        )

    return ValidationVerdict(
        passed=True,
        block_reason=None,
        confidence_delta=-disagree_penalty,
        consistency_note=(
            f"LLM classified {llm_class!r} but rule engine classified "
            f"{rule_class!r} from the same signals — LLM may have over-inferred; "
            "applying confidence penalty"
        ),
    )
