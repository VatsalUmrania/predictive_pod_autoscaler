"""
NEXUS Deterministic RCA Baseline
================================
Provides deterministic, zero-dependency ground-truth baseline comparison for
RCAValidator to detect hallucinations in LLM reasoning.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nexus.graph.rca.result import RCAResult

if TYPE_CHECKING:
    from nexus.reasoning.incident_cluster import IncidentCluster

_RULES: list[tuple[frozenset[str], bool, dict[str, Any]]] = [
    (
        frozenset({"env_contract_violation"}),
        False,
        {
            "root_cause": "Required environment variables are missing from the deployment.",
            "failure_class": "config_error",
            "healing_level": 0,
            "confidence": 0.95,
            "reasoning": "Deterministic ENV contract violation.",
        },
    ),
    (
        frozenset({"pod_oomkilled"}),
        False,
        {
            "root_cause": "Pod terminated by the OOM killer — memory limit exceeded.",
            "failure_class": "resource_exhaustion",
            "healing_level": 1,
            "confidence": 0.92,
            "reasoning": "OOMKilled event detected on pod.",
        },
    ),
    (
        frozenset({"pod_crashloop", "deploy_event"}),
        True,
        {
            "root_cause": "Pod crash loop correlated with a recent deployment.",
            "failure_class": "bad_deploy",
            "healing_level": 2,
            "confidence": 0.85,
            "reasoning": "CrashLoop coinciding with recent deploy.",
        },
    ),
    (
        frozenset({"high_error_rate", "deploy_event"}),
        True,
        {
            "root_cause": "HTTP error rate spike following deployment rollout.",
            "failure_class": "bad_deploy",
            "healing_level": 2,
            "confidence": 0.84,
            "reasoning": "Error rate spike correlates with deploy event.",
        },
    ),
    (
        frozenset({"pod_crashloop"}),
        False,
        {
            "root_cause": "Pod repeatedly crashing on startup.",
            "failure_class": "bad_deploy",
            "healing_level": 1,
            "confidence": 0.82,
            "reasoning": "CrashLoopBackOff detected on pod.",
        },
    ),
    (
        frozenset({"db_connection_exhaustion"}),
        False,
        {
            "root_cause": "Database connection pool is exhausted.",
            "failure_class": "resource_exhaustion",
            "healing_level": 2,
            "confidence": 0.80,
            "reasoning": "Database connections saturated.",
        },
    ),
    (
        frozenset({"dns_resolution_failure"}),
        False,
        {
            "root_cause": "DNS resolution failure in namespace.",
            "failure_class": "dependency_failure",
            "healing_level": 1,
            "confidence": 0.78,
            "reasoning": "DNS lookup timeout/refusal.",
        },
    ),
    (
        frozenset({"lambda_oom"}),
        False,
        {
            "root_cause": "Lambda execution exceeded allocated memory limit.",
            "failure_class": "resource_exhaustion",
            "healing_level": 2,
            "confidence": 0.88,
            "reasoning": "Lambda runtime memory limit exceeded.",
        },
    ),
    (
        frozenset({"lambda_timeout"}),
        False,
        {
            "root_cause": "Lambda duration approaching configured execution timeout.",
            "failure_class": "resource_exhaustion",
            "healing_level": 2,
            "confidence": 0.78,
            "reasoning": "Duration near timeout threshold.",
        },
    ),
]


def deterministic_baseline_rca(cluster: IncidentCluster) -> RCAResult:
    """Evaluate cluster signals against deterministic baseline rules."""
    signal_types = cluster.signal_types

    for required, partial_ok, tmpl in _RULES:
        if partial_ok:
            matched = bool(required & signal_types)
        else:
            matched = required.issubset(signal_types)

        if matched:
            return RCAResult(
                root_cause=tmpl["root_cause"],
                failure_class=tmpl["failure_class"],
                healing_level=tmpl["healing_level"],
                confidence=tmpl["confidence"],
                reasoning=tmpl["reasoning"],
                source="rule_based",
            )

    return RCAResult(
        root_cause="Unable to determine root cause from available baseline signals.",
        failure_class="unknown",
        healing_level=0,
        confidence=0.50 if signal_types else 0.30,
        reasoning="No baseline rule matched.",
        source="rule_based",
    )
