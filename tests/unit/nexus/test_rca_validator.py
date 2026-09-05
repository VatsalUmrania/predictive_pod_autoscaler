"""
Tests for nexus.reasoning.rca_validator — RCAValidator harness.

Covers:
  - ConsistencyCheck: LLM vs rule engine agreement/disagreement
  - EvidenceGate: required signals per failure_class
  - ActionEvidenceGate: required signals per suggested_action
  - CascadingDiversityCheck: cascading_failure requires ≥2 agents
  - downgrade_rca helper
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from nexus.bus.incident_event import AgentType, IncidentEvent, Severity, SignalType
from nexus.reasoning.incident_cluster import IncidentCluster
from nexus.reasoning.rca_engine import RCAResult
from nexus.reasoning.rca_validator import (
    RCAValidator,
    ValidationVerdict,
    downgrade_rca,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _event(signal_type: str, agent: str = "k8s", namespace: str = "default") -> IncidentEvent:
    return IncidentEvent(
        agent=agent,
        signal_type=signal_type,
        severity=Severity.WARNING,
        namespace=namespace,
        resource_name="shop-demo",
    )


def _cluster(*signal_types: str, agents: list[str] | None = None) -> IncidentCluster:
    """Build a minimal IncidentCluster with the given signal types."""
    agents = agents or ["k8s"] * len(signal_types)
    first_event = _event(signal_types[0], agents[0])
    cluster = IncidentCluster.new(first_event)
    for sig, ag in zip(signal_types[1:], agents[1:]):
        cluster.add_event(_event(sig, ag))
    return cluster


def _rca(
    failure_class: str = "bad_deploy",
    healing_level: int = 2,
    confidence: float = 0.85,
    suggested_action: str | None = None,
    source: str = "gemini",
) -> RCAResult:
    return RCAResult(
        root_cause="Test root cause",
        failure_class=failure_class,
        healing_level=healing_level,
        runbook_id=None,
        confidence=confidence,
        reasoning="LLM reasoning text",
        source=source,
        suggested_action=suggested_action,
    )


# ── Validator instance used by all tests ──────────────────────────────────────

validator = RCAValidator()


# ── Rule-based RCA: always PASS ───────────────────────────────────────────────

class TestRuleBasedBypass:
    def test_rule_based_rca_always_passes(self):
        cluster = _cluster("pod_crashloop")
        rca = _rca(source="rule_based")
        verdict = validator.validate(cluster, rca)
        assert verdict.passed
        assert verdict.block_reason is None
        assert verdict.confidence_delta == 0.0
        assert "no LLM validation needed" in verdict.consistency_note


# ── ConsistencyCheck ──────────────────────────────────────────────────────────

class TestConsistencyCheck:
    def test_llm_unknown_always_passes(self):
        """LLM classifying unknown is conservative — no penalty."""
        cluster = _cluster("high_error_rate")
        rca = _rca(failure_class="unknown", healing_level=0)
        verdict = validator.validate(cluster, rca)
        assert verdict.passed
        assert verdict.confidence_delta == 0.0

    def test_llm_and_rule_agree(self):
        """pod_crashloop + deploy_event → rule says bad_deploy; LLM says bad_deploy → PASS."""
        cluster = _cluster("pod_crashloop", "deploy_event")
        rca = _rca(failure_class="bad_deploy", healing_level=2)
        verdict = validator.validate(cluster, rca)
        assert verdict.passed
        # Should have near-zero penalty (only potential evidence gate noise)
        assert verdict.confidence_delta >= -0.20, (
            f"Expected small penalty for agreeing RCA, got {verdict.confidence_delta}"
        )

    def test_llm_disagrees_with_definite_rule(self):
        """
        OOMKilled → rule says resource_exhaustion; LLM says bad_deploy (no deploy event)
        → consistency penalty.
        """
        cluster = _cluster("pod_oomkilled")
        rca = _rca(failure_class="bad_deploy", healing_level=2)
        verdict = validator.validate(cluster, rca)
        # Should have a penalty for disagreement
        assert verdict.confidence_delta < 0.0

    def test_llm_disagrees_with_rule_unknown(self):
        """Rule engine returns unknown; LLM makes a specific claim → half-penalty."""
        # deployment_degraded alone → rule says unknown
        cluster = _cluster("deployment_degraded")
        rca = _rca(failure_class="bad_deploy", healing_level=2)
        verdict = validator.validate(cluster, rca)
        # Half-penalty for disagreement when rule is unknown + evidence gate penalty
        assert verdict.confidence_delta < 0.0


# ── EvidenceGate ──────────────────────────────────────────────────────────────

class TestEvidenceGate:
    def test_bad_deploy_with_both_signals_passes(self):
        """pod_crashloop + deploy_event → bad_deploy evidence gate passes."""
        cluster = _cluster("pod_crashloop", "deploy_event")
        rca = _rca(failure_class="bad_deploy", healing_level=2)
        verdict = validator.validate(cluster, rca)
        # May have small consistency note but should not be blocked
        assert verdict.block_reason is None

    def test_bad_deploy_missing_deploy_event_penalised(self):
        """
        pod_crashloop without deploy_event → bad_deploy partially evidenced
        (has must_have_any but not must_also_have) → partial penalty.
        """
        cluster = _cluster("pod_crashloop")
        rca = _rca(failure_class="bad_deploy", healing_level=2)
        verdict = validator.validate(cluster, rca)
        assert verdict.confidence_delta < 0.0
        assert "deploy_event" in verdict.evidence_gaps

    def test_bad_deploy_no_primary_signal_blocked(self):
        """
        deployment_degraded alone → no pod_crashloop/high_error_rate
        → bad_deploy has zero primary evidence → BLOCKED.
        """
        cluster = _cluster("deployment_degraded")
        rca = _rca(failure_class="bad_deploy", healing_level=2)
        verdict = validator.validate(cluster, rca)
        assert verdict.block_reason is not None
        assert not verdict.passed

    def test_resource_exhaustion_with_oomkilled_passes(self):
        cluster = _cluster("pod_oomkilled")
        rca = _rca(failure_class="resource_exhaustion", healing_level=1)
        verdict = validator.validate(cluster, rca)
        assert verdict.block_reason is None

    def test_resource_exhaustion_no_resource_signal_blocked(self):
        """high_error_rate alone cannot evidence resource_exhaustion."""
        cluster = _cluster("high_error_rate")
        rca = _rca(failure_class="resource_exhaustion", healing_level=1)
        verdict = validator.validate(cluster, rca)
        assert verdict.block_reason is not None

    def test_dependency_failure_with_dns_signal_passes(self):
        cluster = _cluster("dns_resolution_failure")
        rca = _rca(failure_class="dependency_failure", healing_level=1)
        verdict = validator.validate(cluster, rca)
        assert verdict.block_reason is None

    def test_unknown_class_never_blocked(self):
        """unknown class has no evidence requirements — never blocked."""
        cluster = _cluster("deployment_degraded")
        rca = _rca(failure_class="unknown", healing_level=0)
        verdict = validator.validate(cluster, rca)
        assert verdict.block_reason is None
        assert verdict.passed


# ── ActionEvidenceGate ────────────────────────────────────────────────────────

class TestActionEvidenceGate:
    def test_restart_pod_requires_pod_failure_signal(self):
        """restart_pod without any pod-failure signal → blocked."""
        cluster = _cluster("high_error_rate")
        rca = _rca(failure_class="bad_deploy", healing_level=1, suggested_action="restart_pod")
        verdict = validator.validate(cluster, rca)
        assert verdict.block_reason is not None
        assert "restart_pod" in verdict.block_reason

    def test_restart_pod_with_crashloop_passes(self):
        cluster = _cluster("pod_crashloop", "deploy_event")
        rca = _rca(failure_class="bad_deploy", healing_level=1, suggested_action="restart_pod")
        verdict = validator.validate(cluster, rca)
        assert verdict.block_reason is None

    def test_rollback_requires_failure_signal(self):
        """kubectl_rollout_undo from deployment_degraded alone → blocked (no crashloop/high_error)."""
        cluster = _cluster("deployment_degraded")
        rca = _rca(
            failure_class="bad_deploy",
            healing_level=3,
            suggested_action="kubectl_rollout_undo",
        )
        verdict = validator.validate(cluster, rca)
        # Should be blocked — both evidence gate and action gate will fire
        assert not verdict.passed

    def test_emit_alert_never_blocked(self):
        """emit_alert has no signal requirement — always passes action gate."""
        cluster = _cluster("deployment_degraded")
        rca = _rca(failure_class="unknown", healing_level=0, suggested_action="emit_alert")
        verdict = validator.validate(cluster, rca)
        # emit_alert action gate always passes; may still have class evidence penalty
        # but should not be BLOCKED due to the action
        # (block would only come from class gate, not action gate for emit_alert)
        action_gaps = [g for g in verdict.evidence_gaps if g in {"emit_alert"}]
        assert not action_gaps


# ── CascadingDiversityCheck ───────────────────────────────────────────────────

class TestCascadingDiversityCheck:
    def test_cascading_single_agent_blocked(self):
        """cascading_failure from 1 agent → blocked."""
        cluster = _cluster("high_error_rate", "pod_crashloop", agents=["k8s", "k8s"])
        rca = _rca(failure_class="cascading_failure", healing_level=2)
        verdict = validator.validate(cluster, rca)
        assert verdict.block_reason is not None
        assert "diversity" in verdict.block_reason or "cascading" in verdict.block_reason

    def test_cascading_multi_agent_passes_check(self):
        """cascading_failure from 2+ agents → diversity check passes."""
        cluster = _cluster(
            "high_error_rate", "pod_crashloop",
            agents=["metrics", "k8s"],
        )
        rca = _rca(failure_class="cascading_failure", healing_level=2)
        verdict = validator.validate(cluster, rca)
        # Diversity check passes; may still have other penalties from evidence gate
        assert "cascading_failure has agent diversity" in verdict.consistency_note or \
               verdict.block_reason is None or \
               "cascading" not in (verdict.block_reason or "")


# ── downgrade_rca ─────────────────────────────────────────────────────────────

class TestDowngradeRca:
    def test_downgrade_sets_unknown_l0(self):
        original = _rca(failure_class="bad_deploy", healing_level=2)
        downgraded = downgrade_rca(original, "test reason")
        assert downgraded.failure_class == "unknown"
        assert downgraded.healing_level == 0
        assert downgraded.runbook_id is None
        assert downgraded.suggested_action is None
        assert "VALIDATION BLOCKED" in downgraded.reasoning
        assert "test reason" in downgraded.reasoning

    def test_downgrade_preserves_root_cause(self):
        original = _rca(failure_class="bad_deploy")
        downgraded = downgrade_rca(original, "missing evidence")
        assert downgraded.root_cause == original.root_cause

    def test_downgrade_preserves_source(self):
        original = _rca(source="openai")
        downgraded = downgrade_rca(original, "reason")
        assert downgraded.source == "openai"


# ── Total penalty cap ─────────────────────────────────────────────────────────

class TestPenaltyCap:
    def test_penalty_capped_at_040(self):
        """Multiple failing checks cannot push delta below -0.40."""
        # Worst case: wrong class, missing primary signal, cascading single-agent
        cluster = _cluster("deployment_degraded")
        rca = _rca(failure_class="cascading_failure", healing_level=3,
                   suggested_action="kubectl_rollout_undo")
        verdict = validator.validate(cluster, rca)
        assert verdict.confidence_delta >= -0.40, (
            f"Penalty exceeded cap: {verdict.confidence_delta}"
        )
