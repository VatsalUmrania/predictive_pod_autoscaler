"""
Deterministic / neuro-symbolic heuristic rules for multi-agent RCA fallback.
"""

from __future__ import annotations

from typing import Any

from nexus.graph.rca.result import RCAResult


def deterministic_synthesis(
    log_report: dict[str, Any],
    metrics_report: dict[str, Any],
    topology_report: dict[str, Any],
    all_signatures: set[str],
    platform: str,
    target_name: str,
    target_ns: str,
) -> RCAResult:
    """Dynamic fallback synthesis for test environments where LLM API is unavailable."""
    evidence: list[str] = (
        log_report["findings"]
        + metrics_report["findings"]
        + topology_report["findings"]
    )

    # Case 1: Bad Deployment / Spec Mutation
    is_deploy_related = (
        topology_report.get("has_recent_deploy")
        or "recent_deploy" in all_signatures
        or "spec_mutation" in all_signatures
        or "rollout_stuck" in all_signatures
        or "startup_crash" in all_signatures
    )
    has_crash_or_errors = (
        "crashloop" in all_signatures
        or "pod_crashloop" in all_signatures
        or "elevated_error_rate" in all_signatures
        or "startup_crash" in all_signatures
    )

    if is_deploy_related and has_crash_or_errors:
        return RCAResult(
            root_cause=f"Deployment rollout or container spec modification to {target_name} introduced a fatal regression causing startup crashes.",
            failure_class="bad_deploy",
            healing_level=2,
            confidence=0.88,
            reasoning="Topology and log analysts confirmed deployment rollout or spec mutation coincided with startup failure.",
            source="langgraph_multi_agent",
            domain=platform,
            suggested_action=(
                "k8s_rollback_deployment"
                if platform == "kubernetes"
                else "aws_rollback_lambda_alias"
            ),
            suggested_fix=f"Roll back deployment {target_name} to previous stable revision to remove breaking deployment changes and restore pod readiness.",
            action_params=(
                {"namespace": target_ns, "deployment_name": target_name}
                if platform == "kubernetes"
                else {"function_name": target_name}
            ),
            actions_to_avoid=[
                "k8s_restart_deployment",
                "k8s_scale_deployment",
                "ignore",
            ],
            evidence_citations=evidence,
        )

    # Case 2: Memory Exhaustion / OOMKilled
    if any(
        s in all_signatures
        for s in (
            "oom_event",
            "oom_signal",
            "memory_saturation",
            "pod_oomkilled",
            "lambda_oom",
        )
    ):
        if platform == "kubernetes":
            action = (
                "k8s_patch_resource_limits"
                if topology_report.get("memory_limit")
                else "k8s_restart_deployment"
            )
            params = {"namespace": target_ns, "deployment_name": target_name}
        else:
            action = "aws_update_lambda_memory"
            params = {
                "function_name": target_name,
                "memory_mb": 512,
                "region": target_ns,
            }
        return RCAResult(
            root_cause=f"Resource memory limit exceeded for {target_name}.",
            failure_class="resource_exhaustion",
            healing_level=1,
            confidence=0.92,
            reasoning="Log/metric analysts identified critical memory exhaustion / OOM signatures.",
            source="langgraph_multi_agent",
            domain=platform,
            suggested_action=action,
            suggested_fix=f"Resolve memory exhaustion for {target_name} by adjusting limits or restarting pods.",
            action_params=params,
            actions_to_avoid=["traffic_increase"],
            evidence_citations=evidence,
        )

    # Case 3: Lambda Execution Timeout
    if "lambda_timeout" in all_signatures or "timeout_event" in all_signatures:
        return RCAResult(
            root_cause=f"Lambda function {target_name} execution exceeded timeout limit.",
            failure_class="resource_exhaustion",
            healing_level=1,
            confidence=0.88,
            reasoning="CloudWatch metrics/logs show function runtime approaching maximum duration.",
            source="langgraph_multi_agent",
            domain=platform,
            suggested_action="aws_update_lambda_timeout",
            suggested_fix=f"Increase Lambda function {target_name} execution timeout to 60s to accommodate processing duration.",
            action_params={
                "function_name": target_name,
                "timeout_seconds": 60,
                "region": target_ns,
            },
            evidence_citations=evidence,
        )

    # Case 4: Missing Environment Variables / Config
    if "missing_config" in all_signatures or "env_contract_violation" in all_signatures:
        return RCAResult(
            root_cause=f"Required environment variables or configuration values missing in {target_name}.",
            failure_class="config_error",
            healing_level=2,
            confidence=0.90,
            reasoning="Log analyst detected missing environment variables preventing application startup.",
            source="langgraph_multi_agent",
            domain=platform,
            suggested_action=(
                "k8s_patch_configmap" if platform == "kubernetes" else None
            ),
            suggested_fix=f"Patch configuration or environment variables for {target_name} to resolve startup dependency errors.",
            action_params={"namespace": target_ns, "deployment_name": target_name},
            evidence_citations=evidence,
        )

    # Case 5: Database / Dependency Connection Exhaustion
    if any(
        s in all_signatures
        for s in (
            "connection_exhaustion",
            "database_anomaly",
            "db_connection_exhaustion",
        )
    ):
        return RCAResult(
            root_cause=f"Downstream database connection pool is exhausted for {target_name}.",
            failure_class="resource_exhaustion",
            healing_level=2,
            confidence=0.82,
            reasoning="Log analyst identified connection pool errors and timeout traces.",
            source="langgraph_multi_agent",
            domain=platform,
            suggested_action=(
                "k8s_restart_deployment" if platform == "kubernetes" else None
            ),
            suggested_fix=f"Restart {target_name} to clear stale database pool connections and re-establish downstream pools.",
            action_params={"namespace": target_ns, "deployment_name": target_name},
            actions_to_avoid=["k8s_scale_deployment"],
            evidence_citations=evidence,
        )

    # Case 6: General CrashLoop / Runtime Panics
    if any(
        s in all_signatures for s in ("crashloop", "runtime_panic", "pod_crashloop")
    ):
        return RCAResult(
            root_cause=f"Application pods for {target_name} encountered an unhandled runtime error or crashloop.",
            failure_class="bad_deploy",
            healing_level=1,
            confidence=0.90,
            reasoning="Log analyst identified crashloop backoff trace.",
            source="langgraph_multi_agent",
            domain=platform,
            suggested_action=(
                "k8s_restart_deployment" if platform == "kubernetes" else None
            ),
            action_params={"namespace": target_ns, "deployment_name": target_name},
            evidence_citations=evidence,
        )

    # Case 7: High Error Rate / Traffic Spikes
    if any(
        s in all_signatures
        for s in (
            "elevated_error_rate",
            "high_error_rate",
            "traffic_spike",
            "load_surge",
        )
    ):
        return RCAResult(
            root_cause=f"HTTP error rate or traffic spike in {target_name} exceeding SLO threshold.",
            failure_class="cascading_failure",
            healing_level=1,
            confidence=0.78,
            reasoning="Metrics analyst measured error rate breach or load surge without clear fatal log signatures.",
            source="langgraph_multi_agent",
            domain=platform,
            suggested_action="k8s_scale_resource" if platform == "kubernetes" else None,
            action_params={"namespace": target_ns, "deployment_name": target_name},
            evidence_citations=evidence,
        )

    # Default fallback
    return RCAResult(
        root_cause=f"Anomalous operational signals detected on {target_name}.",
        failure_class="unknown",
        healing_level=0,
        confidence=0.50,
        reasoning="Multi-agent evidence is inconclusive; escalating for human investigation.",
        source="langgraph_multi_agent",
        domain=platform,
        evidence_citations=evidence,
    )
