"""
NEXUS Confidence Scorer
========================
Calibrates raw RCA confidence into a final score governing ActionLadder levels:
    < 0.50 -> L0 only | 0.50-0.70 -> L1 | 0.70-0.85 -> L2 | >= 0.85 -> L3
"""

from __future__ import annotations

import logging

from nexus.graph.rca import RCAResult
from nexus.reasoning.incident_cluster import IncidentCluster

logger = logging.getLogger(__name__)

_CLASS_ADJUSTMENTS = {
    "bad_deploy": 0.05,
    "resource_exhaustion": 0.05,
    "dependency_failure": 0.0,
    "config_error": 0.0,
    "cascading_failure": -0.05,
    "unknown": -0.15,
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
        llm_weight: float = 0.55,
        agreement_weight: float = 0.35,
        class_adj_weight: float = 0.10,
        conservative_bias: float = 0.03,
    ):
        total = llm_weight + agreement_weight + class_adj_weight
        if abs(total - 1.0) > 0.01:
            raise ValueError(
                f"Weights must sum to 1.0, got {total:.2f} "
                f"(llm={llm_weight}, agreement={agreement_weight}, class_adj={class_adj_weight})"
            )
        self._w_llm = llm_weight
        self._w_agreement = agreement_weight
        self._w_class_adj = class_adj_weight
        self._bias = conservative_bias

        # Phase 6: historical boosts injected by FeedbackLoop
        # runbook_id → delta (range ±0.05 from KnowledgeBase)
        self._historical_boosts: dict[str, float] = {}

    def score(
        self,
        cluster: IncidentCluster,
        rca_result: RCAResult,
        external_penalty: float = 0.0,
    ) -> float:
        """
        Compute a calibrated confidence score in [0.0, 1.0].

        For rule-based RCA:
            Returns the rule's confidence directly (already calibrated for each rule).

        For Gemini RCA:
            Blends: LLM confidence + signal agreement + failure class adjustment.

        Args:
            external_penalty: Additional penalty (≥ 0.0) subtracted from the
                              blended score.  Supplied by RCAValidator when the
                              LLM's RCA fails consistency or evidence checks.
                              Applied after all other factors, before clamping.
        """
        source = getattr(rca_result, "source", "langgraph_agent")
        runbook_id = getattr(rca_result, "runbook_id", None) or ""
        failure_class = (
            getattr(rca_result, "failure_type", None)
            or getattr(rca_result, "failure_class", None)
            or ""
        )
        healing_level = int(getattr(rca_result, "healing_level", 1))

        if source == "rule_based":
            # Rule-based scores are pre-calibrated — apply only conservative bias
            raw = rca_result.confidence - self._bias
            # Add historical boost for rule-based too (delta is small, ±0.05)
            boost = self._historical_boosts.get(runbook_id, 0.0)
            return max(0.0, min(1.0, raw + boost))

        # Gemini — blend multiple signals
        llm_conf = rca_result.confidence
        agreement = cluster.signal_agreement_score()

        # Class adjustment: normalize to [0, 1] (shift from [-0.20, +0.05] to [0, 1])
        class_adj_raw = _CLASS_ADJUSTMENTS.get(failure_class, 0.0)
        # Map [-0.20, 0.05] → [0.0, 1.0]
        class_adj_normalized = (class_adj_raw + 0.20) / 0.25

        blended = (
            self._w_llm * llm_conf
            + self._w_agreement * agreement
            + self._w_class_adj * class_adj_normalized
        )

        # Deploy-proximity bonus: deploy correlated with bad_deploy = clearer signal
        if cluster.has_deploy_event and failure_class == "bad_deploy":
            blended += 0.05

        # Phase 6: historical boost from Learning Plane (±0.05, capped)
        hist_boost = self._historical_boosts.get(runbook_id, 0.0)
        blended += hist_boost

        # PPA feature boost: +0.05 if cluster was seeded by a high-confidence
        # LSTM spike prediction with elevated load (rps > 20 OR cpu > 60%)
        blended += self._compute_ppa_feature_boost(cluster)

        # Conservative bias
        blended -= self._bias

        # RCA validation penalty — subtracted after all other factors so the
        # deduction is visible in the debug log as a discrete line item.
        blended -= abs(external_penalty)

        # Cap based on healing level and quorum
        n_agents = len(cluster.agent_types)
        if healing_level == 3 and n_agents < 2:
            # L3 requires at least 2 independent agents
            blended = min(blended, 0.82)

        final = max(0.0, min(1.0, blended))

        logger.debug(
            f"[ConfidenceScorer] "
            f"llm={llm_conf:.2f} agreement={agreement:.2f} "
            f"class_adj={class_adj_raw:+.2f} "
            f"hist_boost={hist_boost:+.3f} "
            f"deploy_bonus={'+0.05' if cluster.has_deploy_event and failure_class == 'bad_deploy' else '0'} "
            f"validation_penalty={-abs(external_penalty):+.2f} "
            f"→ final={final:.2f}"
        )
        return final

    def _compute_ppa_feature_boost(self, cluster: IncidentCluster) -> float:
        """+0.05 if cluster was seeded by a high-confidence PPA spike prediction."""
        if not cluster.ppa_raw_features:
            return 0.0
        rf = cluster.ppa_raw_features
        rps = rf.get("rps_per_replica", 0.0)
        cpu = rf.get("cpu_utilization_pct", 0.0)
        # Elevated load: rps > 20 OR cpu > 60%
        if rps > 20.0 or cpu > 60.0:
            return 0.05
        return 0.0

    def set_historical_boosts(self, boosts: dict[str, float]) -> None:
        """
        Update the historical boost map from the Learning Plane.
        Called by FeedbackLoop every N minutes after the KnowledgeBase is updated.

        Args:
            boosts: Mapping of runbook_id → delta (from KnowledgeBase.get_all_adjustments()).
        """
        self._historical_boosts = dict(boosts)
        logger.info(
            f"[ConfidenceScorer] Historical boosts updated — "
            f"{len(boosts)} runbook(s): "
            + ", ".join(
                f"{k}={v:+.3f}"
                for k, v in sorted(boosts.items(), key=lambda x: -abs(x[1]))
            )[:200]
        )

    def gate(self, confidence: float) -> int:
        """
        Map a confidence score to the maximum permitted healing level.
        Used by the Orchestrator to set the executor's confidence.

        Action Ladder Alignment:
            L1 actions are no-regret (restart already failing pods, flush cache).
            They are permitted whenever evidence indicates a real failure (>= 0.40).
            L2 actions (scale, resize) require solid evidence (>= 0.70).
            L3 actions (rollbacks, reverts) require high confidence (>= 0.85).
        """
        if confidence >= 0.85:
            return 3
        if confidence >= 0.70:
            return 2
        if confidence >= 0.40:
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
