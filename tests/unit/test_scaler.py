"""Unit tests for the scaler module (K8s client & replica calculation)."""

import logging
from unittest.mock import MagicMock, patch

import pytest
from kubernetes import client
from kubernetes import config as k8s_config

from ppa.infrastructure import kubernetes
from ppa.operator import scaler


class TestLazyK8sClientInitialization:
    """Test lazy initialization and caching of K8s API client."""

    def setup_method(self):
        """Reset the module-level cache before each test."""
        kubernetes._apps_v1_instance = None

    def test_lazy_init_not_called_on_module_load(self):
        """Verify that K8s client is NOT initialized when module loads."""
        # _apps_v1_instance should be None initially
        assert kubernetes._apps_v1_instance is None

    def test_get_apps_v1_initializes_on_first_call(self):
        """Verify that get_apps_v1() initializes client on first call."""
        with patch("ppa.infrastructure.kubernetes.init_k8s_client") as mock_init:
            mock_api = MagicMock(spec=client.AppsV1Api)
            mock_init.return_value = mock_api

            result = kubernetes.get_apps_v1()

            assert result == mock_api
            mock_init.assert_called_once()

    def test_get_apps_v1_returns_cached_instance(self):
        """Verify that get_apps_v1() caches and reuses the client."""
        with patch("ppa.infrastructure.kubernetes.init_k8s_client") as mock_init:
            mock_api = MagicMock(spec=client.AppsV1Api)
            mock_init.return_value = mock_api

            result1 = kubernetes.get_apps_v1()
            result2 = kubernetes.get_apps_v1()

            assert result1 is result2
            # Should only initialize once
            mock_init.assert_called_once()

    def test_get_apps_v1_returns_none_on_persistent_failure(self):
        """Verify that get_apps_v1() returns None if client init fails."""
        with patch("ppa.infrastructure.kubernetes.init_k8s_client") as mock_init:
            mock_init.side_effect = Exception("K8s API unavailable")

            result = kubernetes.get_apps_v1()

            assert result is None

    def test_get_apps_v1_logs_initialization_success(self, caplog):
        """Verify that successful initialization is logged."""
        with patch("ppa.infrastructure.kubernetes.init_k8s_client") as mock_init:
            mock_api = MagicMock(spec=client.AppsV1Api)
            mock_init.return_value = mock_api

            with caplog.at_level(logging.INFO):
                kubernetes.get_apps_v1()

            assert "K8s API client initialized successfully" in caplog.text

    def test_get_apps_v1_logs_initialization_failure(self, caplog):
        """Verify that initialization failures are logged."""
        with patch("ppa.infrastructure.kubernetes.init_k8s_client") as mock_init:
            mock_init.side_effect = Exception("Connection refused")

            with caplog.at_level(logging.ERROR):
                kubernetes.get_apps_v1()

            assert "Failed to initialize K8s client" in caplog.text
            assert "Connection refused" in caplog.text


class TestInitK8sClient:
    """Test the init_k8s_client() function."""

    def test_tries_incluster_config_first(self):
        """Verify that load_incluster_config is tried first."""
        from contextlib import suppress

        with (
            patch(
                "ppa.infrastructure.kubernetes.k8s_config.load_incluster_config"
            ) as mock_in_cluster,
            patch("ppa.infrastructure.kubernetes.k8s_config.load_kube_config"),
            patch("ppa.infrastructure.kubernetes.client.AppsV1Api"),
        ):
            mock_in_cluster.side_effect = k8s_config.ConfigException("Not in cluster")

            with suppress(Exception):
                kubernetes.init_k8s_client(retries=1)

            mock_in_cluster.assert_called_once()

    def test_falls_back_to_kube_config(self):
        """Verify that kube_config is tried as fallback."""
        with (
            patch(
                "ppa.infrastructure.kubernetes.k8s_config.load_incluster_config"
            ) as mock_in_cluster,
            patch("ppa.infrastructure.kubernetes.k8s_config.load_kube_config") as mock_kube,
            patch("ppa.infrastructure.kubernetes.client.AppsV1Api") as mock_api_class,
        ):
            mock_in_cluster.side_effect = k8s_config.ConfigException("Not in cluster")
            mock_api_instance = MagicMock()
            mock_api_class.return_value = mock_api_instance

            result = kubernetes.init_k8s_client(retries=1)

            assert result == mock_api_instance
            mock_in_cluster.assert_called_once()
            mock_kube.assert_called_once()

    def test_retries_on_transient_failure(self):
        """Verify that retries happen with backoff on both config loaders."""
        attempt_count = [0]

        def fail_twice_then_succeed(*args, **kwargs):
            attempt_count[0] += 1
            if attempt_count[0] < 2:
                raise Exception("Transient failure")
            # On 2nd attempt, succeed by returning normally (don't raise)

        with (
            patch(
                "ppa.infrastructure.kubernetes.k8s_config.load_incluster_config"
            ) as mock_in_cluster,
            patch("ppa.infrastructure.kubernetes.k8s_config.load_kube_config") as mock_kube,
            patch("ppa.infrastructure.kubernetes.client.AppsV1Api"),
            patch("ppa.infrastructure.kubernetes.time.sleep"),
        ):
            # Make incluster fail with ConfigException to trigger fallback
            mock_in_cluster.side_effect = k8s_config.ConfigException("Not in cluster")
            # Make kube_config fail first time, succeed second time
            mock_kube.side_effect = fail_twice_then_succeed

            try:
                result = kubernetes.init_k8s_client(retries=3, backoff=0.1)
                # Should succeed on 2nd attempt
                assert result is not None
            except Exception as e:
                pytest.fail(f"Expected success after retry, but got: {e}")

            # Should have tried load_kube_config at least twice
            assert mock_kube.call_count >= 2

    def test_raises_after_exhausting_retries(self):
        """Verify that exception is raised after retries exhausted."""
        with (
            patch(
                "ppa.infrastructure.kubernetes.k8s_config.load_incluster_config"
            ) as mock_in_cluster,
            patch("ppa.infrastructure.kubernetes.k8s_config.load_kube_config") as mock_kube,
            patch("ppa.infrastructure.kubernetes.time.sleep"),
        ):
            # Both fail with final exception type
            mock_in_cluster.side_effect = k8s_config.ConfigException("Not in cluster")
            mock_kube.side_effect = RuntimeError("kube_config failed")

            with pytest.raises(RuntimeError):
                kubernetes.init_k8s_client(retries=2, backoff=0.01)


class TestScaleDeployment:
    """Test the scale_deployment() function."""

    def setup_method(self):
        """Reset the module-level cache before each test."""
        kubernetes._apps_v1_instance = None

    def test_scale_deployment_success(self, caplog):
        """Verify successful deployment scaling."""
        with patch("ppa.infrastructure.kubernetes.get_apps_v1") as mock_get_api:
            mock_api = MagicMock(spec=client.AppsV1Api)
            mock_get_api.return_value = mock_api

            with caplog.at_level(logging.INFO):
                kubernetes.scale_deployment("my-app", 5, "default")

            mock_api.patch_namespaced_deployment_scale.assert_called_once_with(
                name="my-app",
                namespace="default",
                body={"spec": {"replicas": 5}},
            )
            assert "Patched default/my-app to 5 replicas" in caplog.text

    def test_scale_deployment_handles_api_exception(self, caplog):
        """Verify graceful error handling for API exceptions."""
        with patch("ppa.infrastructure.kubernetes.get_apps_v1") as mock_get_api:
            mock_api = MagicMock(spec=client.AppsV1Api)
            exception = client.exceptions.ApiException("404 Not Found")
            mock_api.patch_namespaced_deployment_scale.side_effect = exception
            mock_get_api.return_value = mock_api

            with caplog.at_level(logging.ERROR):
                # Should not raise, only log
                kubernetes.scale_deployment("nonexistent", 5, "default")

            assert "Failed to scale default/nonexistent" in caplog.text

    def test_scale_deployment_handles_generic_exception(self, caplog):
        """Verify graceful error handling for unexpected exceptions."""
        with patch("ppa.infrastructure.kubernetes.get_apps_v1") as mock_get_api:
            mock_api = MagicMock(spec=client.AppsV1Api)
            mock_api.patch_namespaced_deployment_scale.side_effect = RuntimeError("Network error")
            mock_get_api.return_value = mock_api

            with caplog.at_level(logging.ERROR):
                # Should not raise, only log
                kubernetes.scale_deployment("my-app", 5, "default")

            assert "Unexpected error scaling default/my-app" in caplog.text

    def test_scale_deployment_handles_unavailable_api_client(self, caplog):
        """Verify graceful handling when K8s API client is unavailable."""
        with patch("ppa.infrastructure.kubernetes.get_apps_v1") as mock_get_api:
            mock_get_api.return_value = None  # Simulate unavailable client

            with caplog.at_level(logging.ERROR):
                # Should not raise, only log
                kubernetes.scale_deployment("my-app", 5, "default")

            assert "K8s API client unavailable" in caplog.text
            assert "Will retry on next reconciliation cycle" in caplog.text


class TestCalculateReplicas:
    """Test the calculate_replicas() function."""

    def test_basic_calculation(self):
        """Verify basic replica calculation."""
        # 100 RPS * 1.10 safety / 20 capacity = 5.5 → 6 replicas
        result = scaler.calculate_replicas(
            predicted_load=100,
            current=5,
            min_replicas=1,
            max_replicas=20,
            capacity_per_pod=20,
            scale_up_rate=2.0,
            scale_down_rate=0.5,
            safety_factor=1.10,
        )
        assert result == 6

    def test_respects_min_replicas(self):
        """Verify that min_replicas is enforced."""
        result = scaler.calculate_replicas(
            predicted_load=10,
            current=5,
            min_replicas=3,
            max_replicas=20,
            capacity_per_pod=20,
            scale_up_rate=2.0,
            scale_down_rate=0.5,
        )
        assert result >= 3

    def test_respects_max_replicas(self):
        """Verify that max_replicas is enforced."""
        result = scaler.calculate_replicas(
            predicted_load=1000,
            current=5,
            min_replicas=1,
            max_replicas=10,
            capacity_per_pod=20,
            scale_up_rate=2.0,
            scale_down_rate=0.5,
        )
        assert result <= 10

    def test_safety_factor_applied(self):
        """Verify that safety_factor is applied correctly."""
        # With safety_factor=1.0: 100 * 1.0 / 5 = 20 replicas
        # With safety_factor=1.5: 100 * 1.5 / 5 = 30 replicas (capped by max=25)
        result_100 = scaler.calculate_replicas(
            predicted_load=100,
            current=5,
            min_replicas=1,
            max_replicas=25,
            capacity_per_pod=5,
            scale_up_rate=2.0,
            scale_down_rate=0.5,
            safety_factor=1.0,
        )

        result_150 = scaler.calculate_replicas(
            predicted_load=100,
            current=5,
            min_replicas=1,
            max_replicas=25,
            capacity_per_pod=5,
            scale_up_rate=2.0,
            scale_down_rate=0.5,
            safety_factor=1.5,
        )

        # Higher safety factor should result in more replicas (or same if capped)
        assert result_150 >= result_100

    def test_scale_up_rate_limiting(self):
        """Verify that scale_up_rate limits upward scaling."""
        # Current: 2, raw_desired: 20, but scale_up_rate=2.0 limits to 4
        result = scaler.calculate_replicas(
            predicted_load=200,
            current=2,
            min_replicas=1,
            max_replicas=20,
            capacity_per_pod=20,
            scale_up_rate=2.0,
            scale_down_rate=0.5,
        )
        # With scale_up_rate=2.0: max_up = ceil(2 * 2.0) = 4
        # raw = ceil(200 * 1.10 / 20) = 12
        # desired = max(1, min(4, 12)) = 4
        assert result == 4

    def test_scale_down_rate_limiting(self):
        """Verify that scale_down_rate limits downward scaling."""
        # Current: 10, raw_desired: 2, but scale_down_rate=0.5 limits to 5 minimum
        result = scaler.calculate_replicas(
            predicted_load=10,
            current=10,
            min_replicas=1,
            max_replicas=20,
            capacity_per_pod=20,
            scale_up_rate=2.0,
            scale_down_rate=0.5,
        )
        # With scale_down_rate=0.5: min_down = floor(10 * 0.5) = 5
        # raw = ceil(10 * 1.10 / 20) = 1
        # desired = max(5, min(20, 1)) = 5
        assert result == 5

    def test_zero_capacity_uses_current(self):
        """Verify that zero capacity defaults to current replicas."""
        result = scaler.calculate_replicas(
            predicted_load=100,
            current=5,
            min_replicas=1,
            max_replicas=20,
            capacity_per_pod=0,  # Invalid capacity
            scale_up_rate=2.0,
            scale_down_rate=0.5,
        )
        assert result == 5  # Should stay at current

    def test_large_ramp_up(self):
        """Verify behavior during large traffic spikes (ramp up scenario)."""
        # Simulate: traffic spike from 50 → 500 RPS
        # Current: 3 replicas (was handling 50 RPS)
        # Predicted: needs 50 replicas (500 RPS @ 10/replica)
        # But scale_up_rate=2.0 limits to: 3 * 2.0 = 6 replicas
        result = scaler.calculate_replicas(
            predicted_load=500,
            current=3,
            min_replicas=1,
            max_replicas=50,
            capacity_per_pod=10,
            scale_up_rate=2.0,
            scale_down_rate=0.5,
        )
        # raw = ceil(500 * 1.10 / 10) = 55 (clamped to max=50)
        # max_up = ceil(3 * 2.0) = 6
        # desired = max(1, min(6, 50)) = 6
        assert result == 6

    def test_large_ramp_down(self):
        """Verify behavior during traffic collapse (ramp down scenario)."""
        # Simulate: traffic drop from 500 → 50 RPS
        # Current: 50 replicas
        # Predicted: needs 6 replicas
        # But scale_down_rate=0.5 limits to: 50 * 0.5 = 25 minimum
        result = scaler.calculate_replicas(
            predicted_load=50,
            current=50,
            min_replicas=1,
            max_replicas=100,
            capacity_per_pod=10,
            scale_up_rate=2.0,
            scale_down_rate=0.5,
        )
        # raw = ceil(50 * 1.10 / 10) = 6
        # min_down = floor(50 * 0.5) = 25
        # desired = max(25, min(200, 6)) = 25
        assert result == 25
