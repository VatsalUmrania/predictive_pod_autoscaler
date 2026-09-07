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
from nexus.graph.state import IncidentGraphState
from nexus.reasoning.incident_cluster import IncidentCluster
from nexus.reasoning.rca_engine import RCAEngine, RCAResult
from nexus.reasoning.rca_validator import RCAValidator, ValidationVerdict, downgrade_rca

logger = logging.getLogger(__name__)


async def diagnose_node(state: IncidentGraphState) -> dict[str, Any]:
    """Perform validated root cause analysis using synthesized telemetry."""
    events = state.get("events", [])
    telemetry = state.get("telemetry", {})
    target = state.get("target", {})
    target_name = target.get("name", "unknown")

    # 1. Synthesize cluster for RCA engine
    from nexus.bus.incident_event import AgentType, IncidentEvent, Severity

    now = datetime.now(timezone.utc)
    cluster = IncidentCluster(
        cluster_id=f"cluster-{state['incident_id'][:8]}",
        created_at=now,
        last_event_at=now,
        events=[],
    )

    for raw_evt in events:
        try:
            if isinstance(raw_evt, IncidentEvent):
                cluster.events.append(raw_evt)
            elif isinstance(raw_evt, dict):
                agent_val = str(raw_evt.get("agent", "k8s")).lower()
                agent_enum = AgentType.K8S
                try:
                    agent_enum = AgentType(agent_val)
                except Exception:
                    agent_enum = AgentType.ORCHESTRATOR

                sev_val = str(raw_evt.get("severity", "warning")).lower()
                sev_enum = Severity.CRITICAL if sev_val == "critical" else Severity.WARNING

                evt = IncidentEvent(
                    agent=agent_enum,
                    signal_type=str(raw_evt.get("signal_type", "threshold_breach")),
                    severity=sev_enum,
                    namespace=str(raw_evt.get("namespace") or target.get("namespace", "default")),
                    resource_name=str(raw_evt.get("resource_name") or target_name),
                )
                cluster.events.append(evt)
        except Exception as evt_exc:
            logger.debug("[Diagnose] Note processing event: %s", evt_exc)

    # 2. Execute RCA analysis
    rca_engine = RCAEngine()
    rca_result: RCAResult = await rca_engine.analyze(cluster)

    # 3. Neuro-symbolic validation via RCAValidator (Evaluator-Optimizer Harness)
    validator = RCAValidator()
    verdict: ValidationVerdict = validator.validate(cluster, rca_result)

    diagnostic_reflections: list[dict[str, Any]] = []
    final_rca = rca_result

    # Evaluator-Optimizer (Reflexion) Loop:
    # If the initial hypothesis was blocked, and an LLM is active,
    # critique the hypothesis back to the LLM to self-correct against real cluster signals.
    if not verdict.passed and rca_engine._provider.is_available() and rca_result.source != "rule_based":
        logger.info(
            "[Diagnose Reflexion] Validator BLOCKED initial RCA (%s: %s). Prompting LLM critic reflection.",
            rca_result.failure_class,
            verdict.block_reason,
        )
        reflection_entry = {
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
            critique=verdict.block_reason or "Evidence gap in proposed failure class or action",
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
    log_errors = any("error" in l.lower() or "exception" in l.lower() or "crash" in l.lower() for l in recent_logs)
    if log_errors and final_rca.confidence < 0.90:
        final_rca.confidence = min(0.95, final_rca.confidence + 0.05)

    diagnosis_dict = {
        "root_cause": final_rca.root_cause,
        "failure_class": final_rca.failure_class,
        "confidence": final_rca.confidence,
        "suggested_action": final_rca.suggested_action,
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

