"""Tests for per-CR circuit breaker isolation (PR#1 fix)."""

import time
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from ppa.config import PROM_FAILURE_THRESHOLD, FeatureVectorException
from ppa.infrastructure.prometheus import (
    _get_circuit_breaker,
    _set_circuit_breaker,
)
from ppa.operator.features import (
    PrometheusCircuitBreakerError,
    build_feature_vector,
)

# Import public functions from operator (backward compat) and private functions from infrastructure
from ppa.operator.prometheus import (
    prom_query,
    prom_query_parallel,
)


@dataclass
class MockCRState:
    """Mock CRState object for testing."""

    prom_failures: int = 0
    prom_last_failure_time: float = 0.0
    metric_cache: dict | None = None
    volatile_metrics: bool = False


class TestCircuitBreakerHelpers:
    """Test _get_circuit_breaker and _set_circuit_breaker helpers."""

    def test_get_circuit_breaker_from_cr_state(self):
        """Test retrieving circuit breaker state from CR state."""
        cr_state = MockCRState(prom_failures=3, prom_last_failure_time=100.0)
        failures, last_time = _get_circuit_breaker(cr_state)
        assert failures == 3
        assert last_time == 100.0

    def test_get_circuit_breaker_fallback_to_module_level(self):
        """Test circuit breaker falls back to module-level globals when cr_state is None."""
        failures, last_time = _get_circuit_breaker(None)
        # Should return module-level globals (may be 0 if no prior failures)
        assert isinstance(failures, int)
        assert isinstance(last_time, float)

    def test_set_circuit_breaker_on_cr_state(self):
        """Test setting circuit breaker state on CR state."""
        cr_state = MockCRState()
        _set_circuit_breaker(cr_state, 5, 200.0)
        assert cr_state.prom_failures == 5
        assert cr_state.prom_last_failure_time == 200.0

    def test_set_circuit_breaker_fallback_to_module_level(self):
        """Test circuit breaker falls back to module-level globals when cr_state is None."""
        # This test just verifies that the function doesn't crash
        _set_circuit_breaker(None, 2, 150.0)
        # Module globals are updated (we can't directly test without accessing module state)


class TestPromQueryWithCRState:
    """Test prom_query with per-CR circuit breaker isolation."""

    @patch("ppa.infrastructure.prometheus.requests.get")
    @patch("ppa.infrastructure.prometheus.get_current_prometheus_url")
    def test_successful_query_resets_cr_state_circuit_breaker(self, mock_get_url, mock_requests):
        """Test that successful query resets CR state circuit breaker."""
        cr_state = MockCRState(prom_failures=2, prom_last_failure_time=100.0)
        mock_get_url.return_value = "http://prom:9090"
        mock_response = MagicMock()
        mock_response.json.return_value = {"data": {"result": [{"value": [1000, "42.5"]}]}}
        mock_requests.return_value = mock_response

        result = prom_query("test_query", cr_state=cr_state)

        assert result == 42.5
        # Circuit breaker should be reset
        assert cr_state.prom_failures == 0
        assert cr_state.prom_last_failure_time == 0.0

    @patch("ppa.infrastructure.prometheus.requests.get")
    @patch("ppa.infrastructure.prometheus.get_current_prometheus_url")
    def test_failed_query_increments_cr_state_failures(self, mock_get_url, mock_requests):
        """Test that failed query increments CR state failure counter (returns None, not exception)."""
        cr_state = MockCRState(prom_failures=0, prom_last_failure_time=0.0)
        mock_get_url.return_value = "http://prom:9090"
        mock_requests.side_effect = Exception("Connection refused")

        result = prom_query("test_query", cr_state=cr_state)

        # On first failure, should return None (not raise exception)
        assert result is None
        # Failure counter should be incremented
        assert cr_state.prom_failures == 1
        assert cr_state.prom_last_failure_time > 0

    @patch("ppa.infrastructure.prometheus.requests.get")
    @patch("ppa.infrastructure.prometheus.get_current_prometheus_url")
    def test_circuit_breaker_isolation_cr1_fails_cr2_succeeds(self, mock_get_url, mock_requests):
        """Test that CR1's circuit breaker failure doesn't affect CR2."""
        # CR1 state: circuit breaker active (too many failures, recent last_failure_time)
        # Set last_failure_time to very recently (current time) so backoff hasn't elapsed
        cr1 = MockCRState(
            prom_failures=PROM_FAILURE_THRESHOLD + 1,
            prom_last_failure_time=time.time(),  # Recent failure
        )

        # CR2 state: fresh, no failures
        cr2 = MockCRState(prom_failures=0, prom_last_failure_time=0.0)

        mock_get_url.return_value = "http://prom:9090"
        mock_response = MagicMock()
        mock_response.json.return_value = {"data": {"result": [{"value": [1000, "100.0"]}]}}
        mock_requests.return_value = mock_response

        # CR1 should raise circuit breaker error (backoff active)
        with pytest.raises(PrometheusCircuitBreakerError):
            prom_query("test_query", cr_state=cr1)

        # CR2 should succeed (no interference from CR1)
        result = prom_query("test_query", cr_state=cr2)
        assert result == 100.0


class TestPromQueryParallelWithCRState:
    """Test prom_query_parallel with per-CR circuit breaker isolation."""

    @patch("ppa.infrastructure.prometheus.prom_query")
    def test_parallel_queries_use_cr_state(self, mock_query):
        """Test that prom_query_parallel passes CR state to individual queries."""
        cr_state = MockCRState(prom_failures=0)
        mock_query.return_value = 10.0

        queries = {
            "cpu": "up{job='app'}",
            "memory": "container_memory_usage_bytes{job='app'}",
            "rps": "rate(requests_total[1m])",
        }

        # Capture the calls to prom_query to verify cr_state is passed
        results = prom_query_parallel(queries, cr_state=cr_state)

        # Verify all queries were called with the correct cr_state
        assert mock_query.call_count == 3
        for call in mock_query.call_args_list:
            # Check that cr_state was passed as keyword argument
            assert call[1].get("cr_state") is cr_state

        # Verify results are collected
        assert len(results) == 3
        assert all(v == 10.0 for v in results.values())

    @patch("ppa.infrastructure.prometheus.prom_query")
    def test_parallel_queries_cr_isolation(self, mock_query):
        """Test that circuit breaker in one CR doesn't affect parallel queries of another CR."""
        cr1 = MockCRState(prom_failures=0)
        cr2 = MockCRState(prom_failures=0)

        # CR1: some queries succeed, some fail
        # CR2: all queries should succeed (independent state)

        def side_effect(query, prom_url=None, cr_state=None):
            if cr_state is cr1 and "memory" in query:
                raise Exception("Memory query failed for CR1")
            return 50.0

        mock_query.side_effect = side_effect

        queries = {
            "cpu": "up{job='app'}",
            "memory": "container_memory_usage_bytes{job='app'}",
        }

        # CR2 queries should succeed despite CR1's failure
        results = prom_query_parallel(queries, cr_state=cr2)
        assert results["cpu"] == 50.0
        assert results["memory"] == 50.0


class TestMultipleCRsIsolation:
    """Integration tests for multiple CRs with different circuit breaker states."""

    @patch("ppa.infrastructure.prometheus.requests.get")
    @patch("ppa.infrastructure.prometheus.get_current_prometheus_url")
    def test_multiple_crs_independent_failure_tracking(self, mock_get_url, mock_requests):
        """Test that multiple CRs track failures independently."""
        cr1 = MockCRState(prom_failures=0)
        cr2 = MockCRState(prom_failures=0)
        cr3 = MockCRState(prom_failures=0)

        mock_get_url.return_value = "http://prom:9090"

        # Make requests fail then succeed
        failure_sequence = [
            Exception("Fail 1"),
            Exception("Fail 2"),
            Exception("Fail 3"),
            Exception("Fail 4"),
            MagicMock(json=lambda: {"data": {"result": [{"value": [1000, "25.0"]}]}}),
        ]
        mock_requests.side_effect = failure_sequence

        # CR1 and CR2 each fail twice
        for _ in range(2):
            prom_query("query1", cr_state=cr1)

        for _ in range(2):
            prom_query("query2", cr_state=cr2)

        # CR3 succeeds (call 5 in sequence)
        result_cr3 = prom_query("query3", cr_state=cr3)

        # Verify independent tracking
        assert cr1.prom_failures == 2, f"CR1 failures: {cr1.prom_failures}, expected 2"
        assert cr2.prom_failures == 2, f"CR2 failures: {cr2.prom_failures}, expected 2"
        assert cr3.prom_failures == 0, f"CR3 failures: {cr3.prom_failures}, expected 0"
        assert result_cr3 == 25.0

    def test_cr_state_none_respects_module_level_state(self):
        """Test that passing None for cr_state uses module-level circuit breaker."""
        # When cr_state=None, should use module-level globals
        with patch("ppa.infrastructure.prometheus.prom_query") as mock_prom_query:
            mock_prom_query.return_value = 35.0

            queries = {"metric1": "up"}
            results = prom_query_parallel(queries, cr_state=None)

            # The queries should still work with module-level state
            assert len(results) == 1


class TestBuildFeatureVectorValidation:
    @patch("ppa.operator.features.prom_query_parallel")
    def test_missing_current_replicas_raises_feature_vector_error(self, mock_parallel):
        mock_parallel.return_value = {
            "requests_per_second": 0.0,
            "cpu_utilization_pct": 0.0,
            "memory_utilization_pct": 12.5,
            "latency_p95_ms": 0.0,
            "active_connections": 0.0,
            "error_rate": 0.0,
            "cpu_acceleration": 0.0,
            "rps_acceleration": 0.0,
            "current_replicas": None,
        }

        with pytest.raises(FeatureVectorException, match="current_replicas"):
            build_feature_vector(
                target_app="test-app",
                namespace="default",
                reference_replicas=1,
                max_replicas=10,
            )

    @patch("ppa.operator.features.prom_query_parallel")
    def test_scale_from_zero_uses_raw_rps_per_replica(self, mock_parallel):
        mock_parallel.return_value = {
            "requests_per_second": 42.0,
            "cpu_utilization_pct": 0.0,
            "memory_utilization_pct": 12.5,
            "latency_p95_ms": 0.0,
            "active_connections": 0.0,
            "error_rate": 0.0,
            "cpu_acceleration": 0.0,
            "rps_acceleration": 0.0,
            "current_replicas": 0.0,
        }

        features, replicas, raw, degraded = build_feature_vector(
            target_app="test-app",
            namespace="default",
            reference_replicas=1,
            max_replicas=10,
            cr_state=MockCRState(),
        )

        assert replicas == 0.0
        assert raw["requests_per_second"] == 42.0
        assert features["rps_per_replica"] == 42.0
        assert degraded == []

    @patch("ppa.operator.features.time_now")
    @patch("ppa.operator.features.prom_query_parallel")
    def test_stale_cached_required_metric_is_rejected(self, mock_parallel, mock_time):
        mock_time.return_value = 100.0
        state = MockCRState(metric_cache={"current_replicas": (3.0, 0.0)})
        mock_parallel.return_value = {
            "requests_per_second": 10.0,
            "cpu_utilization_pct": 20.0,
            "memory_utilization_pct": 12.5,
            "latency_p95_ms": 5.0,
            "active_connections": 0.0,
            "error_rate": 0.0,
            "cpu_acceleration": 0.0,
            "rps_acceleration": 0.0,
            "current_replicas": None,
        }

        with pytest.raises(FeatureVectorException, match="fresh cached"):
            build_feature_vector(
                target_app="test-app",
                namespace="default",
                reference_replicas=1,
                max_replicas=10,
                cr_state=state,
            )
