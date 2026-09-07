"""
NEXUS RCA Validator
====================
Validates an LLM-generated RCAResult against two orthogonal checks before the
result is promoted to a remediation instruction:

    1. ConsistencyCheck — cross-validates the LLM's failure_class and
       healing_level against the deterministic rule engine.  If the rule
       engine has a definite opinion that disagrees with the LLM, the LLM has
       made an inference leap beyond the available evidence.

    2. EvidenceGate — asserts that the cluster's signal_types satisfy the
       minimum evidence bar for the claimed failure_class and the proposed
       suggested_action.  A class with no evidential support is blocked or
       penalised regardless of LLM confidence.

Design principles:
    • Rule-based RCA is always called, but only used to penalise LLM output —
      it never replaces it.  This preserves LLM nuance while anchoring it.
    • The validator is purely synchronous and has no I/O — it reads the cluster
      snapshot and produces a verdict.  Live K8s re-query is the responsibility
      of LiveStateValidator (governance layer).
    • Conservative direction: when in doubt, penalise rather than block.
      Blocking is reserved for clear evidence-class contradictions.
    • rule-based source is always PASS (already conservative by design).

ValidationVerdict fields:
    passed            True unless block_reason is set.
    block_reason      Non-None → orchestrator must downgrade RCA to L0 alert.
    confidence_delta  Negative penalty applied to calibrated score.  0.0 = no change.
    consistency_note  Human-readable explanation (written to audit trail / log).
    evidence_gaps     Signals that were required but absent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from nexus.reasoning.incident_cluster import IncidentCluster
from nexus.reasoning.rca_engine import RCAResult, _rule_based_rca

logger = logging.getLogger(__name__)

# ── Evidence requirements per failure class ───────────────────────────────────
# must_have_any: cluster must contain at least one signal from this set
# must_also_have: cluster must ALSO contain at least one of these (after must_have_any)
# If neither set is specified, the class is always evidence-sufficient (e.g. "unknown")


@dataclass(frozen=True)
class EvidenceRequirement:
    must_have_any: frozenset[str] = field(default_factory=frozenset)
    must_also_have: frozenset[str] = field(default_factory=frozenset)


_EVIDENCE_REQUIREMENTS: dict[str, EvidenceRequirement] = {
    "bad_deploy": EvidenceRequirement(
        must_have_any=frozenset({"pod_crashloop", "high_error_rate", "rollout_stuck"}),
        must_also_have=frozenset({"deploy_event"}),
    ),
    "resource_exhaustion": EvidenceRequirement(
        # At least one deterministic resource signal must be present
        must_have_any=frozenset({"pod_oomkilled", "hpa_maxed", "lambda_oom",
                                  "lambda_throttle_spike", "db_connection_exhaustion"}),
    ),
    "dependency_failure": EvidenceRequirement(
        must_have_any=frozenset({"dns_resolution_failure", "upstream_down",
                                  "sqs_dlq_depth_high", "dynamo_system_error"}),
    ),
    "config_error": EvidenceRequirement(
        must_have_any=frozenset({"env_contract_violation", "secret_committed",
                                  "pod_crashloop"}),
    ),
    "cascading_failure": EvidenceRequirement(
        # cascading requires multi-agent spread — checked separately via agent_count
        must_have_any=frozenset({"high_error_rate", "pod_crashloop",
                                  "deployment_degraded"}),
    ),
    # "unknown" has no requirements — always evidence-sufficient (conservative direction)
    "unknown": EvidenceRequirement(),
}

# Per-action minimum required signals from the triggering cluster.
# Actions that operate on pod/deployment state require direct pod-failure evidence.
_ACTION_EVIDENCE: dict[str, frozenset[str]] = {
    "restart_pod": frozenset({"pod_crashloop", "pod_oomkilled", "pod_pending"}),
    "restart_deployment": frozenset({"pod_crashloop", "pod_oomkilled",
                                     "deployment_degraded", "rollout_stuck"}),
    "k8s_restart_deployment": frozenset({"pod_crashloop", "pod_oomkilled",
                                         "deployment_degraded", "rollout_stuck"}),
    "kubectl_rollout_undo": frozenset({"pod_crashloop", "high_error_rate",
                                       "rollout_stuck"}),
    "rollback_deployment": frozenset({"pod_crashloop", "high_error_rate",
                                      "rollout_stuck"}),
    "k8s_rollback_deployment": frozenset({"pod_crashloop", "high_error_rate",
                                          "rollout_stuck"}),
    "scale_deployment": frozenset({"hpa_maxed", "high_error_rate", "pod_pending"}),
    "k8s_scale_deployment": frozenset({"hpa_maxed", "high_error_rate", "pod_pending"}),
    "scale_resource": frozenset({"hpa_maxed", "high_error_rate", "pod_pending",
                                  "lambda_throttle_spike"}),
    "aws_update_lambda_memory": frozenset({"lambda_oom", "lambda_timeout"}),
    "aws_update_lambda_timeout": frozenset({"lambda_timeout"}),
    "aws_replay_dlq": frozenset({"sqs_dlq_depth_high"}),
    "flush_coredns_cache": frozenset({"dns_resolution_failure"}),
    # Low-blast actions always allowed — no signal gate needed
    "emit_alert": frozenset(),
    "patch_annotation": frozenset(),
    "patch_configmap": frozenset(),
    "k8s_patch_configmap": frozenset(),
    "cordon_node": frozenset(),
    "drain_node": frozenset(),
}

# Penalty applied when LLM and rule engine disagree on failure_class
# (rule engine has a definite non-unknown opinion that differs from LLM)
_DISAGREEMENT_PENALTY: float = 0.15

# Penalty applied when evidence gates find missing signals
_EVIDENCE_GAP_PENALTY: float = 0.20

# Penalty applied when cascading_failure is claimed without multi-agent evidence
_CASCADING_WITHOUT_DIVERSITY_PENALTY: float = 0.15


# ── Result types ──────────────────────────────────────────────────────────────


@dataclass
class ValidationVerdict:
    """
    Result of the RCA validation harness.

    passed:            False only when block_reason is set.
    block_reason:      Non-None means the orchestrator must downgrade this RCA
                       to failure_class="unknown", healing_level=0 (alert only).
    confidence_delta:  Negative value to subtract from the calibrated confidence.
                       Applied by ConfidenceScorer.score() via external_penalty.
    consistency_note:  Plain-English explanation written to audit trail / logs.
    evidence_gaps:     Signal types that were required but not present.
    """

    passed: bool
    block_reason: str | None
    confidence_delta: float  # ≤ 0.0; penalty subtracted from calibrated score
    consistency_note: str
    evidence_gaps: list[str] = field(default_factory=list)

    @property
    def has_penalty(self) -> bool:
        return self.confidence_delta < 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "block_reason": self.block_reason,
            "confidence_delta": round(self.confidence_delta, 3),
            "consistency_note": self.consistency_note,
            "evidence_gaps": self.evidence_gaps,
        }


_VERDICT_PASS = ValidationVerdict(
    passed=True,
    block_reason=None,
    confidence_delta=0.0,
    consistency_note="rule-based source — no LLM validation needed",
)


# ── Validator ─────────────────────────────────────────────────────────────────


class RCAValidator:
    """
    Validates an LLM-produced RCAResult before it enters the confidence
    scoring and remediation pipeline.

    Usage (in Orchestrator._process_cluster):

        verdict = self._rca_validator.validate(cluster, rca_result)
        if verdict.block_reason:
            rca_result = downgrade_rca(rca_result, verdict.block_reason)
        # pass verdict.confidence_delta to ConfidenceScorer.score()

    Args:
        disagreement_penalty:        Subtracted from confidence when LLM and
                                     rule engine disagree (default 0.15).
        evidence_gap_penalty:        Subtracted when required signals are absent
                                     (default 0.20).
        block_on_zero_evidence:      If True, block (not just penalise) when the
                                     claimed failure_class has no supporting signals
                                     at all (default True).
        block_on_cascading_without_diversity: If True, block cascading_failure
                                     claims from single-agent clusters (default True).
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

    # ── Public entry point ────────────────────────────────────────────────────

    def validate(
        self,
        cluster: IncidentCluster,
        rca_result: RCAResult,
    ) -> ValidationVerdict:
        """
        Run all validation checks and return a consolidated ValidationVerdict.

        Rule-based RCA results are trusted as-is (already deterministic) and
        bypass all checks.  Only LLM-sourced RCA results are validated.
        """
        if rca_result.source == "rule_based":
            return _VERDICT_PASS

        # 1. Consistency check (LLM vs rule engine)
        rule_verdict = self._consistency_check(cluster, rca_result)

        # 2. Evidence gate (required signals for claimed class)
        evidence_verdict = self._evidence_gate(cluster, rca_result)

        # 3. Action evidence gate (required signals for suggested_action)
        action_verdict = self._action_evidence_gate(cluster, rca_result)

        # 4. Special: cascading_failure requires agent diversity
        cascade_verdict = self._cascading_diversity_check(cluster, rca_result)

        # Merge verdicts: block wins, then accumulate penalties
        all_verdicts = [rule_verdict, evidence_verdict, action_verdict, cascade_verdict]

        block_reason: str | None = None
        total_penalty: float = 0.0
        notes: list[str] = []
        gaps: list[str] = []

        for v in all_verdicts:
            if v.block_reason:
                block_reason = v.block_reason
            total_penalty += abs(v.confidence_delta)  # all deltas are ≤ 0
            if v.consistency_note:
                notes.append(v.consistency_note)
            gaps.extend(v.evidence_gaps)

        # Cap total penalty at 0.40 to avoid over-correcting
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
                f"(cluster={cluster.cluster_id}, "
                f"llm_class={rca_result.failure_class})"
            )
        elif total_penalty > 0:
            logger.info(
                f"[RCAValidator] Penalty −{total_penalty:.2f} applied — {note} "
                f"(cluster={cluster.cluster_id}, "
                f"llm_class={rca_result.failure_class})"
            )
        else:
            logger.debug(
                f"[RCAValidator] PASS — no issues "
                f"(cluster={cluster.cluster_id})"
            )

        return verdict

    # ── Check 1: Consistency (LLM vs deterministic rule engine) ──────────────

    def _consistency_check(
        self,
        cluster: IncidentCluster,
        rca_result: RCAResult,
    ) -> ValidationVerdict:
        """
        Run _rule_based_rca on the same cluster and compare its failure_class
        to the LLM's output.

        Cases:
            LLM == rule == X          → PASS (strong agreement)
            LLM == X, rule == unknown → WARN (rule has no opinion — light penalty)
            LLM == X, rule == Y (Y ≠ unknown, Y ≠ X) → PENALISE −0.15
            LLM == unknown            → PASS (LLM is conservative)
        """
        rule_rca = _rule_based_rca(cluster)
        llm_class = rca_result.failure_class
        rule_class = rule_rca.failure_class

        # LLM is already conservative
        if llm_class == "unknown":
            return ValidationVerdict(
                passed=True,
                block_reason=None,
                confidence_delta=0.0,
                consistency_note="LLM classified unknown — conservative direction, no penalty",
            )

        # Perfect agreement
        if llm_class == rule_class:
            return ValidationVerdict(
                passed=True,
                block_reason=None,
                confidence_delta=0.0,
                consistency_note=f"LLM and rule engine agree: {llm_class}",
            )

        # Rule engine has no opinion (unknown) — light warning, small penalty
        if rule_class == "unknown":
            return ValidationVerdict(
                passed=True,
                block_reason=None,
                confidence_delta=-self._disagree_penalty * 0.5,
                consistency_note=(
                    f"LLM classified {llm_class} but rule engine returned unknown "
                    f"(insufficient signals for deterministic classification) "
                    f"— applying half-penalty"
                ),
            )

        # Rule engine has a definite but different opinion — penalise
        return ValidationVerdict(
            passed=True,
            block_reason=None,
            confidence_delta=-self._disagree_penalty,
            consistency_note=(
                f"LLM classified {llm_class!r} but rule engine classified "
                f"{rule_class!r} from the same signals — LLM may have over-inferred; "
                f"applying confidence penalty"
            ),
        )

    # ── Check 2: Evidence gate (required signals for claimed class) ────────────

    def _evidence_gate(
        self,
        cluster: IncidentCluster,
        rca_result: RCAResult,
    ) -> ValidationVerdict:
        """
        Assert that cluster.signal_types contains the minimum signals required
        to justify the claimed failure_class.
        """
        req = _EVIDENCE_REQUIREMENTS.get(rca_result.failure_class)
        if req is None or (not req.must_have_any and not req.must_also_have):
            # No requirements defined — class is always evidence-sufficient
            return ValidationVerdict(
                passed=True,
                block_reason=None,
                confidence_delta=0.0,
                consistency_note=f"no evidence requirements defined for {rca_result.failure_class}",
            )

        sigs = cluster.signal_types
        gaps: list[str] = []

        # Check must_have_any
        if req.must_have_any and not (req.must_have_any & sigs):
            gaps.extend(sorted(req.must_have_any))
            if self._block_zero_evidence:
                return ValidationVerdict(
                    passed=False,
                    block_reason=(
                        f"failure_class={rca_result.failure_class!r} requires at least one of "
                        f"{sorted(req.must_have_any)} but none present in cluster signals"
                    ),
                    confidence_delta=-self._evidence_penalty,
                    consistency_note=(
                        f"BLOCKED: {rca_result.failure_class} has no primary evidence "
                        f"signals in cluster"
                    ),
                    evidence_gaps=gaps,
                )
            return ValidationVerdict(
                passed=True,
                block_reason=None,
                confidence_delta=-self._evidence_penalty,
                consistency_note=(
                    f"{rca_result.failure_class} claimed but required signals "
                    f"{sorted(req.must_have_any)} missing — penalising"
                ),
                evidence_gaps=gaps,
            )

        # Check must_also_have (secondary corroboration)
        if req.must_also_have and not (req.must_also_have & sigs):
            missing = sorted(req.must_also_have - sigs)
            gaps.extend(missing)
            return ValidationVerdict(
                passed=True,
                block_reason=None,
                confidence_delta=-self._evidence_penalty * 0.5,
                consistency_note=(
                    f"{rca_result.failure_class} claimed but corroborating signals "
                    f"{missing} absent — partial penalty"
                ),
                evidence_gaps=gaps,
            )

        return ValidationVerdict(
            passed=True,
            block_reason=None,
            confidence_delta=0.0,
            consistency_note=f"evidence gate passed for {rca_result.failure_class}",
        )

    # ── Check 3: Action evidence gate ─────────────────────────────────────────

    def _action_evidence_gate(
        self,
        cluster: IncidentCluster,
        rca_result: RCAResult,
    ) -> ValidationVerdict:
        """
        Assert that cluster.signal_types supports the suggested_action.
        Only applies when rca_result.suggested_action is set; skipped otherwise.
        """
        action = rca_result.suggested_action
        if not action:
            return ValidationVerdict(
                passed=True,
                block_reason=None,
                confidence_delta=0.0,
                consistency_note="no suggested_action — action evidence gate skipped",
            )

        required = _ACTION_EVIDENCE.get(action, frozenset())
        if not required:
            # No requirement for this action — zero-blast actions always fine
            return ValidationVerdict(
                passed=True,
                block_reason=None,
                confidence_delta=0.0,
                consistency_note=f"no signal requirement for action {action!r}",
            )

        sigs = cluster.signal_types
        if required & sigs:
            return ValidationVerdict(
                passed=True,
                block_reason=None,
                confidence_delta=0.0,
                consistency_note=(
                    f"action {action!r} evidence gate passed "
                    f"(signals: {sorted(required & sigs)})"
                ),
            )

        # Action requires specific signals — none present
        if self._block_zero_evidence:
            return ValidationVerdict(
                passed=False,
                block_reason=(
                    f"suggested_action={action!r} requires at least one of "
                    f"{sorted(required)} but none present in cluster signals"
                ),
                confidence_delta=-self._evidence_penalty,
                consistency_note=f"BLOCKED: {action!r} lacks pod-failure evidence",
                evidence_gaps=sorted(required),
            )

        return ValidationVerdict(
            passed=True,
            block_reason=None,
            confidence_delta=-self._evidence_penalty,
            consistency_note=(
                f"action {action!r} requires {sorted(required)} but none present "
                f"— penalising"
            ),
            evidence_gaps=sorted(required),
        )

    # ── Check 4: Cascading failure diversity check ─────────────────────────────

    def _cascading_diversity_check(
        self,
        cluster: IncidentCluster,
        rca_result: RCAResult,
    ) -> ValidationVerdict:
        """
        cascading_failure is only credible when signals come from multiple
        independent agents.  Single-agent cascading_failure is very likely
        a mis-classification.
        """
        if rca_result.failure_class != "cascading_failure":
            return ValidationVerdict(
                passed=True,
                block_reason=None,
                confidence_delta=0.0,
                consistency_note="not cascading_failure — diversity check skipped",
            )

        n_agents = len(cluster.agent_types)
        if n_agents >= 2:
            return ValidationVerdict(
                passed=True,
                block_reason=None,
                confidence_delta=0.0,
                consistency_note=(
                    f"cascading_failure has agent diversity "
                    f"({n_agents} agents) — check passed"
                ),
            )

        if self._block_cascading:
            return ValidationVerdict(
                passed=False,
                block_reason=(
                    f"cascading_failure claimed but only {n_agents} agent(s) "
                    f"contributed signals — insufficient diversity for cascading diagnosis"
                ),
                confidence_delta=-_CASCADING_WITHOUT_DIVERSITY_PENALTY,
                consistency_note="BLOCKED: cascading_failure without multi-agent evidence",
            )

        return ValidationVerdict(
            passed=True,
            block_reason=None,
            confidence_delta=-_CASCADING_WITHOUT_DIVERSITY_PENALTY,
            consistency_note=(
                f"cascading_failure from single agent — penalising "
                f"(need ≥2 independent agents)"
            ),
        )


# ── Convenience helpers used by Orchestrator ──────────────────────────────────


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

    if any(s in signals for s in ("pod_crashloop", "pod_oomkilled", "deployment_degraded", "rollout_stuck")):
        # Observable pod / deployment failure is real even if causal theory was blocked
        symptom_conf = 0.85
        healing_level = 1
        suggested_action = "k8s_restart_deployment"
    elif any(s in signals for s in ("lambda_oom", "lambda_timeout")):
        symptom_conf = 0.80
        healing_level = 2
        suggested_action = "aws_update_lambda_memory" if "lambda_oom" in signals else "aws_update_lambda_timeout"
    elif any(s in signals for s in ("high_error_rate", "apigw_5xx_spike")):
        symptom_conf = 0.75
        healing_level = 1
        suggested_action = "k8s_restart_deployment"
    elif signals:
        # Detected signals exist
        symptom_conf = 0.65
        healing_level = 0

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
        action_params={"resource_name": cluster.primary_resource, "namespace": cluster.namespace} if cluster else {},
        rollback_plan=None,
    )
