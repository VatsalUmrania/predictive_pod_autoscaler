"""
RCA Downgrade logic when validation blocks an unverified hypothesis.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nexus.graph.rca import RCAResult

if TYPE_CHECKING:
    from nexus.reasoning.incident_cluster import IncidentCluster


def downgrade_rca(
    rca_result: RCAResult,
    reason: str,
    cluster: IncidentCluster | None = None,
) -> RCAResult:
    """
    Return a new RCAResult demoting unverified causal claims, while dynamically
    preserving evidence-based symptom confidence and safe L1 platform remediation.
    """
    signals = cluster.signal_types if cluster else set()

    symptom_conf = 0.50
    healing_level = 0
    suggested_action = None

    if any(
        s in signals
        for s in (
            "pod_crashloop",
            "pod_oomkilled",
            "deployment_degraded",
            "rollout_stuck",
        )
    ):
        symptom_conf = 0.85
        healing_level = 1
        suggested_action = "k8s_restart_deployment"
    elif any(s in signals for s in ("lambda_oom", "lambda_timeout")):
        symptom_conf = 0.80
        healing_level = 2
        suggested_action = (
            "aws_update_lambda_memory"
            if "lambda_oom" in signals
            else "aws_update_lambda_timeout"
        )
    elif any(s in signals for s in ("high_error_rate", "apigw_5xx_spike")):
        symptom_conf = 0.75
        healing_level = 1
        suggested_action = "k8s_restart_deployment"
    elif signals:
        symptom_conf = 0.65
        healing_level = 0

    action_params = (
        {"resource_name": cluster.primary_resource, "namespace": cluster.namespace}
        if cluster
        else {}
    )

    return RCAResult(
        root_cause=rca_result.root_cause,
        failure_class="unknown",
        healing_level=healing_level,
        runbook_id=None,
        confidence=symptom_conf,
        reasoning=f"[VALIDATION BLOCKED: {reason}] Original LLM reasoning: {rca_result.reasoning}",
        source=rca_result.source,
        actions_to_avoid=rca_result.actions_to_avoid,
        domain=rca_result.domain,
        suggested_action=suggested_action,
        action_params=action_params,
        rollback_plan=None,
    )
