"""
NEXUS Diagnose Node (Neuro-Symbolic Root Cause Analysis)
========================================================
Diagnoses the incident root cause using the synthesized telemetry context.
Enforces neuro-symbolic validation via RCAValidator to prevent LLM hallucinations,
unsubstantiated claims, and cross-domain mismatches.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from nexus.engine.fsm import IncidentState
from nexus.graph.nodes.diagnose_cluster import build_diagnose_cluster
from nexus.graph.nodes.diagnose_reflexion import run_diagnose_reflexion
from nexus.graph.rca import RCALeadSynthesizer, RCAResult
from nexus.graph.state import IncidentGraphState
from nexus.reasoning.rca_validator import RCAValidator, ValidationVerdict, downgrade_rca

logger = logging.getLogger(__name__)

# Backwards-compatible alias for tests and reflection harness
RCAEngine = RCALeadSynthesizer


async def diagnose_node(state: IncidentGraphState) -> dict[str, Any]:
    """Perform validated root cause analysis using synthesized telemetry."""
    events = state.get("events", [])
    telemetry = state.get("telemetry", {})
    target = state.get("target", {})

    # 1. Synthesize cluster for RCA engine
    cluster = build_diagnose_cluster(
        incident_id=state["incident_id"],
        events=events,
        telemetry=telemetry,
        target=target,
    )

    # 2. Execute Multi-Agent RCA analysis
    rca_engine = RCAEngine()
    if type(rca_engine) is RCALeadSynthesizer:
        rca_result: RCAResult = await rca_engine.investigate(
            telemetry=telemetry,
            events=events,
            target=target,
            platform=state.get("platform", "kubernetes"),
        )
    elif hasattr(rca_engine, "analyze"):
        import inspect
        res = rca_engine.analyze(cluster)
        rca_result = await res if inspect.isawaitable(res) else res
    else:
        import inspect
        res = rca_engine.investigate(
            telemetry=telemetry,
            events=events,
            target=target,
            platform=state.get("platform", "kubernetes"),
        )
        rca_result = await res if inspect.isawaitable(res) else res

    # 3. Neuro-symbolic validation via RCAValidator
    validator = RCAValidator()
    verdict: ValidationVerdict = validator.validate(cluster, rca_result)

    diagnostic_reflections: list[dict[str, Any]] = []
    final_rca = rca_result

    # Evaluator-Optimizer (Reflexion) Loop
    is_engine_available = getattr(
        getattr(rca_engine, "_provider", None), "is_available", lambda: False
    )()
    if (
        not verdict.passed
        and is_engine_available
        and rca_result.source != "rule_based"
        and hasattr(rca_engine, "refine")
    ):
        final_rca, verdict, diagnostic_reflections = await run_diagnose_reflexion(
            cluster=cluster,
            rca_engine=rca_engine,
            rca_result=rca_result,
            validator=validator,
            verdict=verdict,
        )
    elif not verdict.passed:
        logger.warning(
            "[Diagnose] RCAValidator BLOCKED RCA '%s' (reason: %s). Demoting unproven causal claims.",
            rca_result.failure_class,
            verdict.block_reason,
        )
        final_rca = downgrade_rca(
            rca_result,
            reason=f"Blocked by RCAValidator: {verdict.block_reason}",
            cluster=cluster,
        )
    elif verdict.confidence_delta < 0.0:
        calibrated_conf = max(0.1, rca_result.confidence + verdict.confidence_delta)
        logger.info(
            "[Diagnose] RCAValidator PENALISED confidence %.2f -> %.2f (delta=%.2f)",
            rca_result.confidence,
            calibrated_conf,
            verdict.confidence_delta,
        )
        final_rca.confidence = calibrated_conf

    # Enrich with logs context if available
    recent_logs = telemetry.get("recent_logs", [])
    log_snippet = " | ".join(recent_logs[-3:]) if recent_logs else "None"
    log_errors = any(
        "error" in line.lower() or "exception" in line.lower() or "crash" in line.lower()
        for line in recent_logs
    )
    if log_errors and final_rca.confidence < 0.90:
        final_rca.confidence = min(0.95, final_rca.confidence + 0.05)

    diagnosis_dict = {
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

    now_iso = datetime.now(timezone.utc).isoformat()
    audit_entry = {
        "timestamp": now_iso,
        "stage": "diagnose",
        "action": "rca_completed",
        "details": f"Class: {final_rca.failure_class} (conf={final_rca.confidence:.2f})",
    }

    logger.info(
        "[Diagnose] Incident %s diagnosed: class=%s, conf=%.2f, root_cause=%s",
        state["incident_id"],
        final_rca.failure_class,
        final_rca.confidence,
        final_rca.root_cause,
    )

    return {
        "diagnosis": diagnosis_dict,
        "diagnostic_reflections": diagnostic_reflections,
        "fsm_state": IncidentState.PLANNING.value,
        "audit_log": [audit_entry],
    }
