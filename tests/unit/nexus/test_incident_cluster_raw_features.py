"""Unit tests for raw_features rendering in IncidentCluster.to_llm_context()."""


from nexus.bus.incident_event import AgentType, IncidentEvent, Severity, SignalType
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


class TestRawFeaturesInLlMContext:
    def test_llm_context_includes_raw_features_when_present(self):
        """to_llm_context() renders raw_features as a dedicated indented line."""
        event = make_event(
            signal_type=SignalType.TRAFFIC_SPIKE_PREDICTED,
            context={
                "current_rps": 100.0,
                "predicted_rps": 450.0,
                "confidence": 0.88,
                "horizon_minutes": 10,
                "raw_features": {
                    "requests_per_second": 100.0,
                    "rps_per_replica": 50.0,
                    "cpu_utilization_pct": 72.5,
                    "memory_utilization_pct": 60.0,
                },
            },
        )
        cluster = IncidentCluster.new(event)
        ctx = cluster.to_llm_context()

        assert "rps/rep=" in ctx
        assert "cpu=  72.5" in ctx

    def test_llm_context_omits_raw_features_when_absent(self):
        """No raw_features block when context has none."""
        event = make_event(context={"error_rate": 0.15, "threshold": 0.05})
        cluster = IncidentCluster.new(event)
        ctx = cluster.to_llm_context()
        assert "raw:" not in ctx

    def test_llm_context_raw_features_handles_missing_keys(self):
        """Gracefully handles partial raw_features (some keys absent)."""
        event = make_event(
            context={"raw_features": {"rps_per_replica": 40.0}},
        )
        cluster = IncidentCluster.new(event)
        ctx = cluster.to_llm_context()
        assert "rps/rep=" in ctx
        assert "N/A" in ctx

    def test_predicted_rps_appears_in_ctx_kv(self):
        """predicted_rps is in _key_order so it appears in the main signal ctx_kv."""
        event = make_event(
            signal_type=SignalType.TRAFFIC_SPIKE_PREDICTED,
            context={
                "current_rps": 100.0,
                "predicted_rps": 450.0,
                "confidence": 0.88,
                "raw_features": {"rps_per_replica": 50.0},
            },
        )
        cluster = IncidentCluster.new(event)
        ctx = cluster.to_llm_context()
        assert "current_rps=100.000" in ctx

    def test_multiple_events_with_and_without_raw_features(self):
        """Mixed cluster: one event has raw_features, one doesn't — both render cleanly."""
        spike_event = make_event(
            signal_type=SignalType.TRAFFIC_SPIKE_PREDICTED,
            context={
                "confidence": 0.85,
                "raw_features": {"rps_per_replica": 60.0, "cpu_utilization_pct": 80.0},
            },
        )
        error_event = make_event(
            signal_type=SignalType.HIGH_ERROR_RATE,
            context={"error_rate": 0.20, "threshold": 0.05},
        )
        cluster = IncidentCluster.new(spike_event)
        cluster.add_event(error_event)
        ctx = cluster.to_llm_context()
        assert "rps/rep=" in ctx
        assert "TRAFFIC_SPIKE_PREDICTED" in ctx
        assert "HIGH_ERROR_RATE" in ctx
