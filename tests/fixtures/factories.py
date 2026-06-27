"""Factory fixtures for building complex test objects.

Eliminates boilerplate when creating feature vectors, predictions, CR states, etc.
"""

import pytest
from ppa.operator.models import ReconciliationRequest

from ppa.common.feature_spec import FEATURE_COLUMNS
from ppa.domain import CRState


@pytest.fixture
def feature_vector_factory():
    """Factory for building feature vectors with sensible defaults.

    Usage:
        features = feature_vector_factory()  # All defaults
        features = feature_vector_factory(rps_per_replica=25.0)  # Override
    """

    def _create(**overrides) -> dict[str, float]:
        """Build feature vector with given overrides."""
        defaults = {
            "rps_per_replica": 10.5,
            "cpu_utilization_pct": 45.2,
            "memory_utilization_pct": 60.0,
            "latency_p95_ms": 150.0,
            "active_connections": 100.0,
            "network_rx_bps": 1024000.0,
            "network_tx_bps": 2048000.0,
            "disk_read_iops": 50.0,
            "disk_write_iops": 75.0,
            "gc_pause_ms": 10.5,
            "request_queue_depth": 5.0,
            "error_rate_pct": 0.1,
            "cache_hit_ratio": 0.85,
            "temporal_hour": 14.0,  # 2 PM
        }

        defaults.update(overrides)

        # Validate all required features present
        if not all(col in defaults for col in FEATURE_COLUMNS):
            missing = set(FEATURE_COLUMNS) - set(defaults.keys())
            raise ValueError(f"Missing features: {missing}")

        return {col: defaults[col] for col in FEATURE_COLUMNS}

    return _create


@pytest.fixture
def prediction_factory():
    """Factory for building prediction results with optional accuracy stats.

    Usage:
        pred = prediction_factory()  # Predict 10.0 RPS
        pred = prediction_factory(predicted_rps=50.0, mape_pct=5.0)
    """

    def _create(
        predicted_rps: float = 10.0,
        mape_pct: float | None = None,
        concept_drift_detected: bool = False,
        inference_latency_ms: float = 2.5,
    ) -> dict:
        """Build prediction result."""
        result = {
            "predicted_rps": predicted_rps,
            "inference_latency_ms": inference_latency_ms,
            "concept_drift_detected": concept_drift_detected,
        }

        if mape_pct is not None:
            result["mape_pct"] = mape_pct

        return result

    return _create


@pytest.fixture
def cr_state_factory():
    """Factory for building CRState objects with realistic defaults.

    Usage:
        state = cr_state_factory()  # All defaults
        state = cr_state_factory(current_replicas=5, max_replicas=20)
    """

    def _create(
        cr_name: str = "test-app",
        current_replicas: int = 3,
        min_replicas: int = 1,
        max_replicas: int = 10,
        target_app: str = "test-app",
        target_namespace: str = "default",
        container_name: str = "test-app",
        model_dir: str = "/tmp/models",
        target_horizon: str = "rps_t3m",
    ) -> CRState:
        """Build CRState with given parameters."""
        return CRState(
            cr_name=cr_name,
            namespace=target_namespace,
            current_replicas=current_replicas,
            min_replicas=min_replicas,
            max_replicas=max_replicas,
            target_app=target_app,
            target_namespace=target_namespace,
            container_name=container_name,
            last_desired_replicas=current_replicas,
            stable_count=0,
            prom_failures=0,
            prom_last_failure_time=None,
            model_dir=model_dir,
            target_horizon=target_horizon,
            predictor=None,  # Will be set if needed
        )

    return _create


@pytest.fixture
def reconciliation_request_factory(feature_vector_factory, prediction_factory):
    """Factory for building ReconciliationRequest with complete context.

    Usage:
        req = reconciliation_request_factory()  # Defaults
        req = reconciliation_request_factory(
            current_replicas=10,
            predicted_rps=50.0
        )
    """

    def _create(
        cr_name: str = "test-app",
        namespace: str = "default",
        current_replicas: int = 3,
        predicted_rps: float = 10.0,
        features: dict | None = None,
        model_ready: bool = True,
        prom_circuit_open: bool = False,
    ) -> ReconciliationRequest:
        """Build ReconciliationRequest."""
        if features is None:
            features = feature_vector_factory(
                rps_per_replica=predicted_rps / current_replicas
            )

        prediction = prediction_factory(predicted_rps=predicted_rps)

        return ReconciliationRequest(
            cr_name=cr_name,
            namespace=namespace,
            current_replicas=current_replicas,
            features=features,
            prediction=prediction,
            model_ready=model_ready,
            prom_circuit_open=prom_circuit_open,
        )

    return _create


@pytest.fixture
def scaling_config_factory():
    """Factory for building ScalingConfig with various presets.

    Usage:
        config = scaling_config_factory()  # Default production
        config = scaling_config_factory(preset="conservative")
        config = scaling_config_factory(scale_up_rate=3.0)  # Override
    """
    from ppa.operator.config_models import ScalingConfig

    def _create(
        preset: str = "default",
        min_replicas: int = 1,
        max_replicas: int = 10,
        scale_up_rate: float = 2.0,
        scale_down_rate: float = 0.5,
        safety_factor: float = 1.1,
        stabilization_steps: int = 3,
        **overrides,
    ) -> ScalingConfig:
        """Build ScalingConfig with optional preset."""

        presets = {
            "default": {
                "min_replicas": 1,
                "max_replicas": 10,
                "scale_up_rate": 2.0,
                "scale_down_rate": 0.5,
                "safety_factor": 1.1,
                "stabilization_steps": 3,
            },
            "conservative": {
                "min_replicas": 1,
                "max_replicas": 10,
                "scale_up_rate": 1.5,  # Slower scale-up
                "scale_down_rate": 0.75,  # Slower scale-down
                "safety_factor": 1.2,  # Higher buffer
                "stabilization_steps": 5,  # More stable
            },
            "aggressive": {
                "min_replicas": 1,
                "max_replicas": 10,
                "scale_up_rate": 4.0,  # Fast scale-up
                "scale_down_rate": 0.25,  # Even faster down
                "safety_factor": 1.0,  # No buffer
                "stabilization_steps": 1,  # Immediate
            },
        }

        if preset not in presets:
            raise ValueError(f"Unknown preset: {preset}")

        config_dict = presets[preset].copy()
        config_dict.update(overrides)

        return ScalingConfig(**config_dict)

    return _create


@pytest.fixture
def feature_error_scenarios_factory():
    """Factory for building various feature extraction error scenarios.

    Usage:
        error_scenarios = feature_error_scenarios_factory()
        for scenario_name, feature_dict in error_scenarios.items():
            test_validation(feature_dict)
    """

    def _create() -> dict[str, dict[str, float]]:
        """Build dict of error scenarios."""
        base = {
            "rps_per_replica": 10.5,
            "cpu_utilization_pct": 45.2,
            "memory_utilization_pct": 60.0,
            "latency_p95_ms": 150.0,
            "active_connections": 100.0,
            "network_rx_bps": 1024000.0,
            "network_tx_bps": 2048000.0,
            "disk_read_iops": 50.0,
            "disk_write_iops": 75.0,
            "gc_pause_ms": 10.5,
            "request_queue_depth": 5.0,
            "error_rate_pct": 0.1,
            "cache_hit_ratio": 0.85,
            "temporal_hour": 14.0,
        }

        scenarios = {
            "valid": base.copy(),
            "nan_value": {**base, "rps_per_replica": float("nan")},
            "inf_value": {**base, "latency_p95_ms": float("inf")},
            "negative_inf": {**base, "active_connections": float("-inf")},
            "out_of_range_high": {**base, "cpu_utilization_pct": 150.0},
            "out_of_range_low": {**base, "cpu_utilization_pct": -10.0},
            "zero_replicas": {**base, "rps_per_replica": 0.0},
            "empty_dict": {},
            "all_zeros": dict.fromkeys(base, 0.0),
            "all_max_values": {**base, "cpu_utilization_pct": 100.0},
        }

        return scenarios

    return _create


@pytest.fixture
def prediction_accuracy_scenarios_factory(prediction_factory):
    """Factory for generating prediction accuracy scenarios for testing.

    Usage:
        scenarios = prediction_accuracy_scenarios_factory()
        for scenario, prediction in scenarios.items():
            test_concept_drift_detection(prediction)
    """

    def _create() -> dict[str, dict]:
        """Build dict of accuracy scenarios."""
        return {
            "excellent": prediction_factory(predicted_rps=50.0, mape_pct=1.0),
            "good": prediction_factory(predicted_rps=50.0, mape_pct=3.0),
            "acceptable": prediction_factory(predicted_rps=50.0, mape_pct=5.0),
            "poor": prediction_factory(predicted_rps=50.0, mape_pct=15.0),
            "unacceptable": prediction_factory(predicted_rps=50.0, mape_pct=25.0),
            "zero_error": prediction_factory(predicted_rps=50.0, mape_pct=0.0),
            "drift_detected": prediction_factory(
                predicted_rps=50.0, mape_pct=10.0, concept_drift_detected=True
            ),
        }

    return _create


@pytest.fixture
def load_scenarios_factory():
    """Factory for realistic load scenarios for scaling tests.

    Usage:
        scenarios = load_scenarios_factory()
        for load_name, load_info in scenarios.items():
            check_scaling_decision(load_info["rps"])
    """

    def _create() -> dict[str, dict]:
        """Build dict of load scenarios."""
        return {
            "idle": {"rps": 5.0, "expected_replicas": 1},
            "light": {"rps": 100.0, "expected_replicas": 2},
            "moderate": {"rps": 500.0, "expected_replicas": 10},
            "heavy": {"rps": 2000.0, "expected_replicas": 40},
            "spike": {"rps": 5000.0, "expected_replicas": 100},
            "sustained_high": {"rps": 10000.0, "expected_replicas": 200},
        }

    return _create
