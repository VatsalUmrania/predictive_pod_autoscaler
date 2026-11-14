"""
Reflexion loop helper for diagnose_node.
"""

from __future__ import annotations

import logging
from typing import Any

from nexus.graph.rca import RCAResult
from nexus.reasoning.incident_cluster import IncidentCluster
from nexus.reasoning.rca_validator import RCAValidator, ValidationVerdict, downgrade_rca

logger = logging.getLogger(__name__)


async def run_diagnose_reflexion(
    cluster: IncidentCluster,
    rca_engine: Any,
    rca_result: RCAResult,
    validator: RCAValidator,
    verdict: ValidationVerdict,
) -> tuple[RCAResult, ValidationVerdict, list[dict[str, Any]]]:
    """Execute evaluator-optimizer reflexion loop to refine a blocked hypothesis."""
    diagnostic_reflections: list[dict[str, Any]] = []
    final_rca = rca_result

    logger.info(
        "[Diagnose Reflexion] Validator BLOCKED initial RCA (%s: %s). Prompting LLM critic reflection.",
        rca_result.failure_class,
        verdict.block_reason,
    )
    reflection_entry: dict[str, Any] = {
        "iteration": 1,
        "initial_hypothesis": {
            "failure_class": rca_result.failure_class,
            "root_cause": rca_result.root_cause,
            "suggested_action": rca_result.suggested_action,
        },
        "critique": verdict.block_reason,
        "evidence_gaps": verdict.evidence_gaps,
    }

    refined_result = await rca_engine.refine(
        cluster=cluster,
        previous_rca=rca_result,
        critique=verdict.block_reason
        or "Evidence gap in proposed failure class or action",
        evidence_gaps=verdict.evidence_gaps,
    )

    if refined_result:
        refined_verdict = validator.validate(cluster, refined_result)
        if refined_verdict.passed:
            logger.info(
                "[Diagnose Reflexion] Refined hypothesis PASSED validation: %s (conf=%.2f)",
                refined_result.failure_class,
                refined_result.confidence,
            )
            final_rca = refined_result
            verdict = refined_verdict
            reflection_entry["outcome"] = "refined_accepted"
            reflection_entry["refined_hypothesis"] = {
                "failure_class": refined_result.failure_class,
                "root_cause": refined_result.root_cause,
                "suggested_action": refined_result.suggested_action,
            }
        else:
            logger.warning(
                "[Diagnose Reflexion] Refined hypothesis failed validation (%s). Falling back to conservative downgrade.",
                refined_verdict.block_reason,
            )
            reflection_entry["outcome"] = "refinement_rejected_fallback"
            final_rca = downgrade_rca(
                refined_result,
                reason=f"Refined hypothesis blocked: {refined_verdict.block_reason}",
                cluster=cluster,
            )
    else:
        reflection_entry["outcome"] = "refinement_unavailable_fallback"
        final_rca = downgrade_rca(
            rca_result,
            reason=f"Blocked by RCAValidator: {verdict.block_reason}",
            cluster=cluster,
        )

    diagnostic_reflections.append(reflection_entry)
    return final_rca, verdict, diagnostic_reflections
