"""Unit tests for nexus.reasoning.confidence_scorer ConfidenceScorer PPA bonus."""

from unittest.mock import MagicMock

import pytest

from nexus.bus.incident_event import AgentType, IncidentEvent, Severity, SignalType
from nexus.reasoning.confidence_scorer import ConfidenceScorer
from nexus.reasoning.incident_cluster import IncidentCluster


def make_event(
    signal_type=SignalType.HIGH_ERROR_RATE,
    context=None,
    namespace="default",
    resource="api",
):
    return IncidentEvent(
        agent=AgentType.K8S,
        signal_type=signal_type,
        severity=Severity.WARNING,
        namespace=namespace,
        resource_name=resource,
        context=context or {},
        correlation_id="test-123",
        confidence=0.7,
    )


def make_cluster(events=None):
    cluster = IncidentCluster.new(make_event())
    for evt in (events or []):
        cluster.add_event(evt)
    return cluster


class TestPpaAlignmentBonus:
    """PPA alignment bonus: +0.05 when a high-confidence LSTM spike prediction is present."""

    @pytest.mark.asyncio
    async def test_ppa_high_confidence_boosts_score(self):
        scorer = ConfidenceScorer()
        scorer._historical_boosts = {}

        spike_event = IncidentEvent(
            agent=AgentType.ORCHESTRATOR,
            signal_type=SignalType.TRAFFIC_SPIKE_PREDICTED,
            severity=Severity.WARNING,
            namespace="default",
            resource_name="api",
            context={
                "current_rps": 100.0,
                "predicted_rps": 450.0,
                "confidence": 0.88,
                "raw_features": {"rps_per_replica": 40.0},
            },
            correlation_id="spike-1",
            confidence=0.88,
        )
        cluster = make_cluster([spike_event])

        rca_result = MagicMock()
        rca_result.source = "gemini"
        rca_result.confidence = 0.65
        rca_result.failure_class = "resource_exhaustion"
        rca_result.healing_level = 2
        rca_result.runbook_id = None

        score_with_spike = scorer.score(cluster, rca_result)

        # Without spike event — same score without PPA bonus
        no_spike_cluster = IncidentCluster.new(make_event())
        score_without = scorer.score(no_spike_cluster, rca_result)

        # PPA bonus (+0.05) should make score_with > score_without
        assert score_with_spike > score_without, (
            f"PPA bonus should increase score: {score_with_spike:.3f} vs {score_without:.3f}"
        )

    @pytest.mark.asyncio
    async def test_ppa_low_confidence_no_boost(self):
        scorer = ConfidenceScorer()
        scorer._historical_boosts = {}

        spike_event = IncidentEvent(
            agent=AgentType.ORCHESTRATOR,
            signal_type=SignalType.TRAFFIC_SPIKE_PREDICTED,
            severity=Severity.WARNING,
            namespace="default",
            resource_name="api",
            context={
                "current_rps": 100.0,
                "predicted_rps": 130.0,
                "confidence": 0.50,
                "raw_features": {},
            },
            correlation_id="spike-2",
            confidence=0.50,
        )
        cluster = make_cluster([spike_event])

        rca_result = MagicMock()
        rca_result.source = "gemini"
        rca_result.confidence = 0.65
        rca_result.failure_class = "resource_exhaustion"
        rca_result.healing_level = 2
        rca_result.runbook_id = None

        score = scorer.score(cluster, rca_result)
        baseline = sum([
            0.55 * 0.65,
            0.35 * cluster.signal_agreement_score(),
            0.10 * ((0.0 + 0.20) / 0.25),
        ])
        assert abs(score - (baseline - scorer._bias)) < 0.01

    @pytest.mark.asyncio
    async def test_ppa_bonus_stacks_with_historical_boost(self):
        """Both PPA bonus and historical boost apply in the same score call."""
        scorer = ConfidenceScorer()
        scorer._historical_boosts = {"runbook": 0.03}

        spike_event = IncidentEvent(
            agent=AgentType.ORCHESTRATOR,
            signal_type=SignalType.TRAFFIC_SPIKE_PREDICTED,
            severity=Severity.WARNING,
            namespace="default",
            resource_name="api",
            context={"confidence": 0.90, "raw_features": {}},
            correlation_id="spike-3",
            confidence=0.90,
        )
        base_event = IncidentEvent(
            agent=AgentType.METRICS,
            signal_type=SignalType.HIGH_ERROR_RATE,
            severity=Severity.WARNING,
            namespace="default",
            resource_name="api",
            context={"error_rate": 0.05, "threshold": 0.05},
            correlation_id="base",
            confidence=0.7,
        )

        rca_result = MagicMock()
        rca_result.source = "gemini"
        rca_result.confidence = 0.65
        rca_result.failure_class = "resource_exhaustion"
        rca_result.healing_level = 2
        rca_result.runbook_id = "runbook"

        cluster_with_spike = IncidentCluster.new(base_event)
        cluster_with_spike.add_event(spike_event)

        cluster_without_spike = IncidentCluster.new(base_event)

        score_with = scorer.score(cluster_with_spike, rca_result)
        score_without = scorer.score(cluster_without_spike, rca_result)

        # PPA bonus (+0.05) + historical (+0.03) should make score_with > score_without
        assert score_with > score_without + 0.001
