"""
NEXUS Collect Telemetry Node (Deterministic Fallback Context Acquisition)
========================================================================
Gathers target metrics, recent error logs, and live configuration through the
platform adapter when the autonomous diagnostic agent activates fallback.
"""

from __future__ import annotations

import inspect
import logging
from datetime import datetime, timezone
from typing import Any

from nexus.engine.fsm import IncidentState
from nexus.graph.platform import TargetResource, get_platform_registry
from nexus.graph.rca.result import RCAResult
from nexus.graph.state import IncidentGraphState
from nexus.reasoning.rca_validator import RCAValidator, downgrade_rca

logger = logging.getLogger(__name__)


async def collect_telemetry_node(state: IncidentGraphState) -> dict[str, Any]:
    """Deterministically collect diagnostic signals via the platform adapter."""
    registry = get_platform_registry()
    platform_id = state.get("platform", "kubernetes")
    adapter = registry.get(platform_id) or registry.resolve_adapter(
        state.get("events", [])
    )
    raw_target = state.get("target") or {}
    target = (
        TargetResource(**raw_target) if isinstance(raw_target, dict) else raw_target
    )

    try:
        res = adapter.collect_telemetry(target)
        telemetry = await res if inspect.isawaitable(res) else res
        telemetry_dict = (
            telemetry.model_dump()
            if hasattr(telemetry, "model_dump")
            else (telemetry if isinstance(telemetry, dict) else {})
        )
    except Exception as exc:
        logger.warning("[CollectTelemetry] Adapter failed: %s", exc)
        telemetry_dict = {
            "target": target.model_dump() if hasattr(target, "model_dump") else {},
            "metrics": {},
            "recent_logs": [f"Telemetry error: {exc}"],
            "live_config": {},
            "health_status": "error",
            "raw_signals": state.get("events", []),
        }

    now_iso = datetime.now(timezone.utc).isoformat()
    audit_entry = {
        "timestamp": now_iso,
        "stage": "collect_telemetry",
        "action": "telemetry_acquired",
        "details": f"Logs: {len(telemetry_dict.get('recent_logs', []))} lines | Status: {telemetry_dict.get('health_status')}",
    }

    result: dict[str, Any] = {
        "telemetry": telemetry_dict,
        "fsm_state": IncidentState.PLANNING.value,
        "audit_log": [audit_entry],
    }

    if state.get("fallback_active") or not state.get("diagnosis"):
        events = state.get("events", [])
        target_dict = (
            raw_target
            if isinstance(raw_target, dict)
            else (raw_target.model_dump() if hasattr(raw_target, "model_dump") else {})
        )
        target_name = target_dict.get("name", "workload")
        target_ns = target_dict.get("namespace", "default")
        recent_logs = telemetry_dict.get("recent_logs", [])
        combined_logs = " ".join(str(line) for line in recent_logs).lower()
        combined_events = " ".join(str(e) for e in events).lower()

        if (
            "outofmemory" in combined_logs
            or "oom" in combined_logs
            or "oom" in combined_events
        ):
            fail_class = "resource_exhaustion"
            cause = f"Memory exhaustion / OOMKilled in {target_name}"
            suggested_act = (
                "aws_update_lambda_memory"
                if platform_id == "aws"
                else "k8s_restart_deployment"
            )
        elif "crashloop" in combined_logs or "crash" in combined_events:
            fail_class = "bad_deploy"
            cause = f"Container crash loop detected in {target_name}"
            suggested_act = (
                "k8s_restart_deployment"
                if platform_id == "kubernetes"
                else (
                    "gcp_restart_compute_instance"
                    if platform_id == "gcp"
                    else "aws_update_lambda_memory"
                )
            )
        elif "timeout" in combined_logs or "deadline" in combined_logs:
            fail_class = "dependency_failure"
            cause = f"Downstream dependency timeout in {target_name}"
            suggested_act = (
                "k8s_restart_deployment"
                if platform_id == "kubernetes"
                else "aws_get_log_events"
            )
        else:
            fail_class = "resource_exhaustion"
            cause = f"Telemetry anomaly detected in {target_name}"
            suggested_act = (
                "k8s_restart_deployment"
                if platform_id == "kubernetes"
                else (
                    "gcp_restart_compute_instance"
                    if platform_id == "gcp"
                    else "aws_update_lambda_memory"
                )
            )

        if (
            platform_id not in ("aws", "kubernetes")
            and adapter
            and hasattr(adapter, "get_tools")
            and adapter.get_tools()
        ):
            suggested_act = adapter.get_tools()[0].name

        rca_res = RCAResult(
            root_cause=cause,
            failure_class=fail_class,
            healing_level=1,
            confidence=0.9,
            reasoning=f"Deterministic telemetry inspection identified {cause}",
            domain=platform_id,
            suggested_action=suggested_act,
            action_params={"namespace": target_ns, "deployment_name": target_name},
            evidence_citations=[
                f"Telemetry: logs={len(recent_logs)}, status={telemetry_dict.get('health_status')}"
            ],
        )

        try:
            from nexus.graph.nodes.diagnose_cluster import build_diagnose_cluster

            cluster = build_diagnose_cluster(
                incident_id=state["incident_id"],
                events=events,
                telemetry=telemetry_dict,
                target=target_dict,
            )
            validator = RCAValidator()
            verdict = validator.validate(cluster, rca_res)
            final_rca = rca_res
            if not verdict.passed:
                final_rca = downgrade_rca(
                    rca_res, reason=f"Blocked: {verdict.block_reason}", cluster=cluster
                )
            elif verdict.confidence_delta < 0.0:
                final_rca.confidence = max(
                    0.1, rca_res.confidence + verdict.confidence_delta
                )
        except Exception:
            final_rca = rca_res

        result["diagnosis"] = {
            "root_cause": final_rca.root_cause,
            "failure_class": final_rca.failure_class,
            "confidence": final_rca.confidence,
            "suggested_action": final_rca.suggested_action,
            "suggested_fix": getattr(final_rca, "suggested_fix", None),
            "action_params": final_rca.action_params,
            "reasoning": final_rca.reasoning,
            "evidence_citations": final_rca.evidence_citations,
        }

    return result
