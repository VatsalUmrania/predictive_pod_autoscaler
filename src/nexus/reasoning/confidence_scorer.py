"""
NEXUS Confidence Scorer
========================
Calibrates the raw confidence value from the RCA result into a final
score that governs the healing action level permitted by the ActionLadder.

Score bands (align with ActionLadder healing levels):
    < 0.50   → L0 only (emit_alert, patch_annotation)
    0.50-0.70 → L1 allowed (restart_pod, flush_coredns_cache)
    0.70-0.85 → L2 allowed (scale_deployment)
    ≥ 0.85    → L3 allowed (kubectl_rollout_undo, webhook) — or human approval

Calibration factors (Phase 4):
    1. LLM raw confidence       (weight 0.50)
    2. Signal agreement score   (weight 0.30)
    3. Failure class adjustment (additive ± 0.05—0.20)
    4. Deploy-proximity bonus   (+ 0.05 if deploy correlated with bad_deploy)

Phase 6 (Learning Plane) will add:
    5. Historical runbook success rate from AuditTrail
       (boosts runbooks that have consistently healed similar incidents)

Design decision:
    We bias toward conservative scores. It is better to under-heal and escalate
    to a human than to over-heal and cause a cascading failure.
    The LLM's own confidence estimate is treated as an input signal,
    not taken at face value — Gemini can be overconfident.
"""

from __future__ import annotations

import logging
from typing import Optional

from nexus.reasoning.incident_cluster import IncidentCluster
from nexus.reasoning.rca_engine import RCAResult

logger = logging.getLogger(__name__)

# ── Additive adjustments by failure class ─────────────────────────────────────
# Positive: class is inherently clearer / easier to confirm
# Negative: class is ambiguous or has wider blast radius

_CLASS_ADJUSTMENTS = {
    "config_error":          +0.05,   # ENV violations are deterministic — boost
    "bad_deploy":             0.00,   # Clear signal when deploy is correlated
    "resource_exhaustion":    0.00,   # Metrics are usually reliable
    "dependency_failure":    -0.05,   # DNS/network failures can be transient
    "cascading_failure":     -0.10,   # Complex — risky to intervene autonomously
    "unknown":               -0.20,   # No idea — extremely conservative
}

# Healing level caps — don't let a class automatically grant L3 without signal strength
_MAX_CONFIDENCE_WITHOUT_QUORUM = {
    0: 1.0,   # L0 — alerts always fine
    1: 0.90,
    2: 0.80,
    3: 0.75,  # L3 requires agent diversity too
}


class ConfidenceScorer:
    """
    Calibrates healing confidence from RCA + cluster signals.

    Args:
        llm_weight:       Weight for LLM raw confidence (default 0.55).
        agreement_weight: Weight for signal agreement score (default 0.35).
        class_adj_weight: Weight for failure class adjustment (default 0.10).
        conservative_bias: Subtract this from all scores as conservative bias (default 0.03).
    """

    def __init__(
        self,
        llm_weight:        float = 0.55,
        agreement_weight:  float = 0.35,
        class_adj_weight:  float = 0.10,
        conservative_bias: float = 0.03,
    ):
        total = llm_weight + agreement_weight + class_adj_weight
        if abs(total - 1.0) > 0.01:
            raise ValueError(
                f"Weights must sum to 1.0, got {total:.2f} "
                f"(llm={llm_weight}, agreement={agreement_weight}, class_adj={class_adj_weight})"
            )
        self._w_llm       = llm_weight
        self._w_agreement = agreement_weight
        self._w_class_adj = class_adj_weight
        self._bias        = conservative_bias

    def score(
        self,
        cluster:    IncidentCluster,
        rca_result: RCAResult,
    ) -> float:
        """
        Compute a calibrated confidence score in [0.0, 1.0].

        For rule-based RCA:
            Returns the rule's confidence directly (already calibrated for each rule).

        For Gemini RCA:
            Blends: LLM confidence + signal agreement + failure class adjustment.
        """
        if rca_result.source == "rule_based":
            # Rule-based scores are pre-calibrated — apply only conservative bias
            raw = rca_result.confidence - self._bias
            return max(0.0, min(1.0, raw))

        # Gemini — blend multiple signals
        llm_conf  = rca_result.confidence
        agreement = cluster.signal_agreement_score()

        # Class adjustment: normalize to [0, 1] (shift from [-0.20, +0.05] to [0, 1])
        class_adj_raw = _CLASS_ADJUSTMENTS.get(rca_result.failure_class, 0.0)
        # Map [-0.20, 0.05] → [0.0, 1.0]
        class_adj_normalized = (class_adj_raw + 0.20) / 0.25

        blended = (
            self._w_llm       * llm_conf  +
            self._w_agreement * agreement +
            self._w_class_adj * class_adj_normalized
        )

        # Deploy-proximity bonus: deploy correlated with bad_deploy = clearer signal
        if cluster.has_deploy_event and rca_result.failure_class == "bad_deploy":
            blended += 0.05

        # Conservative bias
        blended -= self._bias

        # Cap based on healing level and quorum
        n_agents = len(cluster.agent_types)
        if rca_result.healing_level == 3 and n_agents < 2:
            # L3 requires at least 2 independent agents
            blended = min(blended, 0.82)

        final = max(0.0, min(1.0, blended))

        logger.debug(
            f"[ConfidenceScorer] "
            f"llm={llm_conf:.2f} agreement={agreement:.2f} "
            f"class_adj={class_adj_raw:+.2f} "
            f"deploy_bonus={'+0.05' if cluster.has_deploy_event and rca_result.failure_class == 'bad_deploy' else '0'} "
            f"→ final={final:.2f}"
        )
        return final

    def gate(self, confidence: float) -> int:
        """
        Map a confidence score to the maximum permitted healing level.
        Used by the Orchestrator to set the executor's confidence.

            < 0.50 → L0  (alert only)
            0.50-0.70 → L1
            0.70-0.85 → L2
            ≥ 0.85  → L3
        """
        if confidence >= 0.85:
            return 3
        if confidence >= 0.70:
            return 2
        if confidence >= 0.50:
            return 1
        return 0

    def describe(self, confidence: float) -> str:
        """Human-readable description of the confidence band."""
        level = self.gate(confidence)
        labels = {
            0: "low — alert only (L0)",
            1: "moderate — no-regret actions allowed (L1)",
            2: "good — bounded mitigation allowed (L2)",
            3: "high — significant changes allowed (L3)",
        }
        return f"{confidence:.2f} [{labels[level]}]"
