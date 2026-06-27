"""Operator-specific fixtures for reconciliation and service tests."""

from unittest.mock import MagicMock

import pytest
from ppa.operator.feature_service import FeatureService
from ppa.operator.reconciliation_service import ReconciliationService
from ppa.operator.scaling_strategy import ProductionScalingStrategy


@pytest.fixture
def mock_prometheus_client():
    """Mock Prometheus client for feature extraction tests.

    Usage:
        client = mock_prometheus_client()
        client.query(...)  # Returns mock response
    """

    def _create(
        default_value: float = 100.0,
        query_count: int = 14,  # Number of features
    ) -> MagicMock:
        """Build mock Prometheus client."""

        client = MagicMock()

        # Setup query responses for each feature
        responses = [
            {
                "resultType": "vector",
                "result": [
                    {
                        "metric": {"pod": "test-app-abc123"},
                        "value": [1234567890.0, str(default_value + i)],
                    }
                ],
            }
            for i in range(query_count)
        ]

        client.query_range = MagicMock(side_effect=responses)
        client.query = MagicMock(side_effect=responses)

        return client

    return _create


@pytest.fixture
def mock_predictor():
    """Mock Predictor for reconciliation tests.

    Usage:
        predictor = mock_predictor()
        predictor.predict(features)  # Returns prediction
    """

    def _create(
        ready: bool = True,
        predicted_load: float = 100.0,
        load_variance: float = 0.1,
        concept_drift: bool = False,
    ) -> MagicMock:
        """Build mock Predictor."""

        predictor = MagicMock()
        predictor.ready.return_value = ready
        predictor.predict.return_value = predicted_load
        predictor.get_prediction_accuracy.return_value = 3.0  # MAPE %
        predictor.has_concept_drift.return_value = concept_drift

        return predictor

    return _create


@pytest.fixture
def mock_feature_service(mock_prometheus_client):
    """Mock FeatureService for reconciliation tests.

    Usage:
        service = mock_feature_service()
        features = await service.extract_features("cr-name")
    """

    def _create(
        circuit_open: bool = False,
        fallback_used: bool = False,
        extract_delay_ms: float = 5.0,
    ) -> MagicMock:
        """Build mock FeatureService."""

        service = MagicMock(spec=FeatureService)

        if circuit_open:
            service.extract_features.return_value = {
                "features": {
                    "rps_per_replica": 10.5,
                    "cpu_utilization_pct": 45.0,
                },
                "current_replicas": 3,
                "fallback_used": True,
                "circuit_open": True,
            }
        else:
            service.extract_features.return_value = {
                "features": {
                    "rps_per_replica": 10.5,
                    "cpu_utilization_pct": 45.0,
                },
                "current_replicas": 3,
                "fallback_used": fallback_used,
                "circuit_open": False,
            }

        return service

    return _create


@pytest.fixture
def mock_scaling_strategy():
    """Mock ProductionScalingStrategy for reconciliation tests.

    Usage:
        strategy = mock_scaling_strategy()
        replicas = strategy.compute_replicas(current=3, prediction=50.0)
    """

    def _create(
        desired_replicas: int = 5,
        rate_limited: bool = False,
        reached_max: bool = False,
    ) -> MagicMock:
        """Build mock ScalingStrategy."""

        strategy = MagicMock(spec=ProductionScalingStrategy)

        strategy.compute_replicas.return_value = {
            "desired_replicas": desired_replicas,
            "rate_limited": rate_limited,
            "reached_max": reached_max,
            "reason": "Scaling to match predicted load",
        }

        return strategy

    return _create


@pytest.fixture
def mock_reconciliation_service(
    mock_feature_service,
    mock_predictor,
    mock_scaling_strategy,
):
    """Mock ReconciliationService for integration tests.

    Usage:
        service = mock_reconciliation_service()
        result = await service.reconcile(...)
    """

    def _create() -> MagicMock:
        """Build mock ReconciliationService."""

        service = MagicMock(spec=ReconciliationService)

        service.reconcile.return_value = {
            "cr_name": "test-app",
            "desired_replicas": 5,
            "applied": True,
            "reason": "Scaling decision applied successfully",
        }

        return service

    return _create


@pytest.fixture
def reconciliation_context_factory(
    feature_vector_factory,
    prediction_factory,
    cr_state_factory,
):
    """Factory for building complete reconciliation context.

    Usage:
        context = reconciliation_context_factory()
        context = reconciliation_context_factory(
            current_replicas=10,
            predicted_rps=500.0
        )
    """

    def _create(
        cr_name: str = "test-app",
        namespace: str = "default",
        current_replicas: int = 3,
        predicted_rps: float = 50.0,
        model_ready: bool = True,
        circuit_open: bool = False,
    ) -> dict:
        """Build complete reconciliation context."""

        features = feature_vector_factory(
            rps_per_replica=predicted_rps / current_replicas
        )
        prediction = prediction_factory(predicted_rps=predicted_rps)
        cr_state = cr_state_factory(
            cr_name=cr_name,
            current_replicas=current_replicas,
        )

        return {
            "cr_name": cr_name,
            "namespace": namespace,
            "current_replicas": current_replicas,
            "features": features,
            "prediction": prediction,
            "model_ready": model_ready,
            "circuit_open": circuit_open,
            "cr_state": cr_state,
        }

    return _create


@pytest.fixture
def scaling_decision_scenarios_factory():
    """Factory for various scaling decision scenarios.

    Usage:
        scenarios = scaling_decision_scenarios_factory()
        for scenario_name, config in scenarios.items():
            test_scaling_decision(config)
    """

    def _create() -> dict:
        """Build dict of scaling decision scenarios."""
        return {
            "no_scaling_needed": {
                "current_replicas": 3,
                "predicted_rps": 150.0,  # 50 RPS per replica
                "capacity_per_pod": 50,
                "expected_replicas": 3,
                "reason": "Current replicas sufficient",
            },
            "scale_up_gradual": {
                "current_replicas": 3,
                "predicted_rps": 500.0,  # Need 10 replicas
                "capacity_per_pod": 50,
                "expected_replicas": 6,  # 2x rate limit: 3 → 6
                "reason": "Scale up limited to 2x",
            },
            "scale_up_max": {
                "current_replicas": 10,
                "predicted_rps": 10000.0,  # Would need 200 if unlimited
                "capacity_per_pod": 50,
                "max_replicas": 100,
                "expected_replicas": 100,  # Capped at max
                "reason": "Reached maximum replicas",
            },
            "scale_down_gradual": {
                "current_replicas": 10,
                "predicted_rps": 50.0,  # Only need 1 replica
                "capacity_per_pod": 50,
                "expected_replicas": 5,  # 0.5x rate limit: 10 → 5
                "reason": "Scale down limited to 0.5x",
            },
            "scale_down_min": {
                "current_replicas": 5,
                "predicted_rps": 10.0,  # Would want <1 if unlimited
                "capacity_per_pod": 50,
                "min_replicas": 1,
                "expected_replicas": 1,  # Capped at min
                "reason": "Clamped to minimum replicas",
            },
            "stabilization_active": {
                "current_replicas": 3,
                "predicted_rps": 500.0,  # Should scale to 6
                "capacity_per_pod": 50,
                "stabilization_steps": 3,
                "consecutive_decisions": 1,  # Only 1 stable decision so far
                "expected_replicas": 3,  # Don't scale yet
                "reason": "Waiting for stabilization window",
            },
        }

    return _create


@pytest.fixture
def error_handling_scenarios_factory():
    """Factory for operator error scenarios.

    Usage:
        scenarios = error_handling_scenarios_factory()
        for scenario_name, config in scenarios.items():
            test_error_recovery(config)
    """

    def _create() -> dict:
        """Build dict of error scenarios."""
        return {
            "prometheus_timeout": {
                "error_type": "PrometheusTimeout",
                "message": "Prometheus query timed out after 10s",
                "should_fallback": True,
                "should_retry": True,
            },
            "prometheus_connection_refused": {
                "error_type": "ConnectionRefusedError",
                "message": "Connection refused to Prometheus",
                "should_fallback": True,
                "should_retry": True,
            },
            "model_load_failed": {
                "error_type": "ModelLoadError",
                "message": "Failed to load TFLite model",
                "should_fallback": False,
                "should_retry": True,
            },
            "k8s_conflict": {
                "error_type": "K8sConflict",
                "message": "Deployment has been modified",
                "should_fallback": False,
                "should_retry": True,
            },
            "k8s_permission_denied": {
                "error_type": "K8sPermissionDenied",
                "message": "Forbidden: User cannot patch deployments",
                "should_fallback": False,
                "should_retry": False,  # Permanent error
            },
            "invalid_features": {
                "error_type": "FeatureValidationError",
                "message": "Feature vector has NaN values",
                "should_fallback": True,
                "should_retry": True,
            },
        }

    return _create
