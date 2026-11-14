"""
NEXUS Collect Telemetry Node (Deterministic Fallback Context Acquisition)
========================================================================
Gathers target metrics, recent error logs, and live configuration through the
platform adapter when the autonomous diagnostic agent activates fallback.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from nexus.engine.fsm import IncidentState
from nexus.graph.nodes.diagnose_cluster import build_diagnose_cluster
from nexus.graph.platform import TargetResource, get_platform_registry
from nexus.graph.rca import RCALeadSynthesizer, RCAResult
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

    logger.info(
        "[CollectTelemetry] Deterministically gathering telemetry for %s on platform %s",
        target,
        platform_id,
    )

    try:
        telemetry = await adapter.collect_telemetry(target)
        telemetry_dict = telemetry.model_dump()
    except Exception as exc:
        logger.warning(
            "[CollectTelemetry] Adapter failed to gather telemetry for %s: %s",
            target,
            exc,
        )
        telemetry_dict = {
            "target": target.model_dump() if hasattr(target, "model_dump") else {},
            "metrics": {},
            "recent_logs": [f"Telemetry collection error: {exc}"],
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

    # If fallback is active or diagnosis is missing, synthesize deterministic fallback RCA
    if state.get("fallback_active") or not state.get("diagnosis"):
        events = state.get("events", [])
        target_dict = (
            raw_target
            if isinstance(raw_target, dict)
            else (raw_target.model_dump() if hasattr(raw_target, "model_dump") else {})
        )

        cluster = build_diagnose_cluster(
            incident_id=state["incident_id"],
            events=events,
            telemetry=telemetry_dict,
            target=target_dict,
        )

        synthesizer = RCALeadSynthesizer()
        rca_res: RCAResult = await synthesizer.investigate(
            telemetry=telemetry_dict,
            events=events,
            target=target_dict,
            platform=platform_id,
        )

        validator = RCAValidator()
        verdict = validator.validate(cluster, rca_res)
        final_rca = rca_res
        if not verdict.passed:
            final_rca = downgrade_rca(
                rca_res,
                reason=f"Blocked by RCAValidator: {verdict.block_reason}",
                cluster=cluster,
            )
        elif verdict.confidence_delta < 0.0:
            final_rca.confidence = max(
                0.1, rca_res.confidence + verdict.confidence_delta
            )

        recent_logs = telemetry_dict.get("recent_logs", [])
        log_snippet = " | ".join(recent_logs[-3:]) if recent_logs else "None"

        result["diagnosis"] = {
            "root_cause": final_rca.root_cause,
            "failure_class": final_rca.failure_class,
            "confidence": final_rca.confidence,
            "suggested_action": final_rca.suggested_action,
            "suggested_fix": getattr(final_rca, "suggested_fix", None),
            "action_params": final_rca.action_params,
            "runbook_id": final_rca.runbook_id,
            "reasoning": final_rca.reasoning,
            "evidence_logs": log_snippet,
            "validator_passed": verdict.passed,
            "validator_block_reason": verdict.block_reason,
            "validator_evidence_gaps": verdict.evidence_gaps,
        }

    return result
