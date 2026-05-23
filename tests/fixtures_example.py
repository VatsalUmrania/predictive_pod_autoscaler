"""Example test demonstrating new fixture usage patterns.

This is NOT a real test file - copy/modify patterns from here for your own tests.
Run: pytest tests/fixtures_example/ --verbose
"""

import pytest

# ============================================================================
# EXAMPLE 1: Unit Test Using Factory Fixtures
# ============================================================================


@pytest.mark.unit
@pytest.mark.scaling
class TestScalingStrategyWithFactories:
    """Demonstrate basic factory usage."""

    def test_scaling_respects_rate_limit(self, scaling_config_factory):
        """Example: Use factory to build config."""
        # Build config with factory
        config = scaling_config_factory(
            scale_up_rate=2.0,
            max_replicas=100,
        )

        # Verify config settings
        assert config.scale_up_rate == 2.0
        assert config.max_replicas == 100

        # Your test logic here
        print(f"✓ Config: scale_up={config.scale_up_rate}x, max={config.max_replicas}")

    def test_scaling_presets(self, scaling_config_factory):
        """Example: Use factory presets."""

        # Get conservative preset
        conservative = scaling_config_factory(preset="conservative")
        assert conservative.scale_up_rate == 1.5

        # Get aggressive preset
        aggressive = scaling_config_factory(preset="aggressive")
        assert aggressive.scale_up_rate == 4.0

        print(f"✓ Conservative: {conservative.scale_up_rate}x up")
        print(f"✓ Aggressive: {aggressive.scale_up_rate}x up")


# ============================================================================
# EXAMPLE 2: Feature Extraction Tests with Scenarios
# ============================================================================


@pytest.mark.unit
@pytest.mark.feature
class TestFeatureExtraction:
    """Demonstrate feature factory usage."""

    def test_valid_features_pass_validation(self, feature_vector_factory):
        """Example: Create valid feature vector."""
        features = feature_vector_factory()

        # All required fields present
        assert len(features) == 14
        assert "rps_per_replica" in features
        assert "cpu_utilization_pct" in features

        print(f"✓ Valid feature vector: {len(features)} features")

    def test_error_scenarios(self, feature_error_scenarios_factory):
        """Example: Use error scenario factory."""
        scenarios = feature_error_scenarios_factory()

        # Test various error cases
        assert "valid" in scenarios
        assert "nan_value" in scenarios
        assert "inf_value" in scenarios

        print(f"✓ {len(scenarios)} error scenarios available")

        # Could iterate and test each
        for scenario_name, _feature_dict in scenarios.items():
            # Simulate validation
            has_error = "nan" in scenario_name or "inf" in scenario_name or "range" in scenario_name
            print(f"  - {scenario_name}: {'ERROR' if has_error else 'VALID'}")


# ============================================================================
# EXAMPLE 3: K8s Mocking for Scaling Tests
# ============================================================================


@pytest.mark.unit
@pytest.mark.operator
class TestK8sScaling:
    """Demonstrate K8s mock usage."""

    def test_scale_success(self, mock_k8s_scale_success):
        """Example: Test successful scaling."""
        setup = mock_k8s_scale_success(
            current_replicas=3,
            target_replicas=5,
        )

        # Get the mock API
        api = setup["api"]

        # Simulate scaling operation
        api.patch_namespaced_deployment(
            name="test-app",
            namespace="default",
            body={"spec": {"replicas": 5}},
        )

        # Verify patch was called
        assert api.patch_namespaced_deployment.called
        print(f"✓ Scale succeeded: {setup['current_replicas']} → {setup['target_replicas']}")

    def test_scale_conflict(self, mock_k8s_scale_conflict):
        """Example: Handle scaling conflict."""
        from kubernetes.client.exceptions import ApiException

        conflict = mock_k8s_scale_conflict()
        api = conflict["api"]

        # Attempting to patch raises conflict
        try:
            api.patch_namespaced_deployment(
                name="test-app",
                namespace="default",
                body={},
            )
            raise AssertionError("Should have raised conflict")
        except ApiException as e:
            assert e.status == 409
            print(f"✓ Conflict handled: {e.status} error")


# ============================================================================
# EXAMPLE 4: Multi-Scenario Testing
# ============================================================================


@pytest.mark.unit
class TestMultipleScenarios:
    """Demonstrate scenario factory patterns."""

    def test_all_load_scenarios(self, load_scenarios_factory):
        """Example: Test across multiple load scenarios."""
        scenarios = load_scenarios_factory()

        for scenario_name, scenario in scenarios.items():
            rps = scenario["rps"]

            # Simulate scaling decision
            actual_replicas = max(1, int(rps / 50))  # Simple formula

            print(f"  ✓ {scenario_name:15} RPS={rps:7} → replicas={actual_replicas}")

    def test_accuracy_scenarios(self, prediction_accuracy_scenarios_factory):
        """Example: Test prediction accuracy handling."""
        scenarios = prediction_accuracy_scenarios_factory()

        for scenario_name, prediction in scenarios.items():
            mape = prediction.get("mape_pct", 0)
            drift = prediction.get("concept_drift_detected", False)

            action = "RETRAIN" if drift or mape > 10 else "OK"
            print(f"  ✓ {scenario_name:15} MAPE={mape:5.1f}% → {action}")


# ============================================================================
# EXAMPLE 5: Parameterized Testing (Best Practice)
# ============================================================================


@pytest.mark.unit
@pytest.mark.parametrize(
    "current_replicas,target_replicas,rate_limit,expected",
    [
        (3, 10, 2.0, 6),  # 2x limit: 3*2 = 6
        (5, 15, 2.0, 10),  # 2x limit: 5*2 = 10
        (10, 3, 0.5, 5),  # 0.5x limit: 10*0.5 = 5
        (100, 1, 0.5, 50),  # 0.5x limit: 100*0.5 = 50
    ],
)
def test_rate_limiting_scenarios(
    current_replicas,
    target_replicas,
    rate_limit,
    expected,
):
    """Example: Parameterized scaling test.

    Runs 4 test cases with different parameters automatically.
    One assertion per scenario.
    """
    # Simulate rate-limited scaling
    max_increase = int(current_replicas * rate_limit)
    actual = min(target_replicas, max_increase)

    # Current is always reachable
    actual = max(current_replicas, actual)

    assert actual >= current_replicas
    yield  # pytest reports each parameter set separately


# ============================================================================
# EXAMPLE 6: Fixture Composition (Advanced)
# ============================================================================


@pytest.mark.unit
@pytest.mark.operator
class TestReconciliationContext:
    """Demonstrate combining multiple fixtures."""

    def test_full_reconciliation_context(
        self,
        feature_vector_factory,
        prediction_factory,
        cr_state_factory,
        scaling_config_factory,
    ):
        """Example: Compose multiple factories for complex test."""

        # Build complete context from factories
        current_replicas = 3
        predicted_rps = 500.0

        features = feature_vector_factory(rps_per_replica=predicted_rps / current_replicas)

        prediction = prediction_factory(
            predicted_rps=predicted_rps,
            mape_pct=3.5,
            concept_drift_detected=False,
        )

        state = cr_state_factory(
            cr_name="test-app",
            current_replicas=current_replicas,
            max_replicas=50,
        )

        config = scaling_config_factory(preset="default")

        # Now have complete context for reconciliation
        print("✓ Context built:")
        print(f"  - Features: {len(features)} features extracted")
        print(f"  - Prediction: {prediction['predicted_rps']} RPS")
        print(f"  - State: {state.current_replicas} current replicas")
        print(f"  - Config: scale_up={config.scale_up_rate}x")


# ============================================================================
# EXAMPLE 7: Testing Error Paths
# ============================================================================


@pytest.mark.unit
def test_error_scenarios(error_handling_scenarios_factory):
    """Example: Iterate through error scenarios."""
    scenarios = error_handling_scenarios_factory()

    for scenario_name, error_info in scenarios.items():
        should_fallback = error_info["should_fallback"]
        should_retry = error_info["should_retry"]

        action = []
        if should_fallback:
            action.append("FALLBACK")
        if should_retry:
            action.append("RETRY")

        print(f"✓ {scenario_name:30} → {', '.join(action)}")


# ============================================================================
# Tests to run:
# ============================================================================

if __name__ == "__main__":
    print("""
    Run these examples with pytest:

    # All examples
    pytest tests/fixtures_example/ -v

    # Just parametrized tests
    pytest tests/fixtures_example/test_rate_limiting -v

    # With output
    pytest tests/fixtures_example/ -v -s

    # Coverage report
    pytest tests/fixtures_example/ --cov=ppa
    """)
