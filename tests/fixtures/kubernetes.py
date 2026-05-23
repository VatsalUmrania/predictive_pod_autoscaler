"""Kubernetes API mock fixtures for operator tests.

Provides realistic mocks for K8s API interactions without requiring a cluster.
"""

from unittest.mock import MagicMock

import pytest
from kubernetes import client


@pytest.fixture
def mock_k8s_deployment():
    """Mock a K8s Deployment object.

    Usage:
        deploy = mock_k8s_deployment()
        deploy = mock_k8s_deployment(name="my-app", replicas=5)
    """

    def _create(
        name: str = "test-app",
        namespace: str = "default",
        current_replicas: int = 3,
        ready_replicas: int = 3,
        updated_replicas: int = 3,
        image: str = "test-app:latest",
        labels: dict = None,
    ) -> MagicMock:
        """Build mock Deployment with realistic structure."""

        if labels is None:
            labels = {"app": name}

        deploy = MagicMock(spec=client.V1Deployment)

        # Metadata
        deploy.metadata.name = name
        deploy.metadata.namespace = namespace
        deploy.metadata.labels = labels
        deploy.metadata.uid = f"uid-{name}"

        # Spec
        deploy.spec.replicas = current_replicas
        deploy.spec.selector.matchLabels = {"app": name}
        deploy.spec.template.spec.containers = [MagicMock(image=image, name=name)]

        # Status
        deploy.status.replicas = current_replicas
        deploy.status.ready_replicas = ready_replicas
        deploy.status.updated_replicas = updated_replicas
        deploy.status.available_replicas = ready_replicas
        deploy.status.conditions = []

        return deploy

    return _create


@pytest.fixture
def mock_k8s_api():
    """Mock K8s AppsV1Api client.

    Usage:
        api = mock_k8s_api()
        api.read_namespaced_deployment("my-app", "default")
    """

    def _create() -> MagicMock:
        """Build mock K8s API client."""
        api = MagicMock(spec=client.AppsV1Api)
        api.read_namespaced_deployment = MagicMock()
        api.patch_namespaced_deployment = MagicMock()
        return api

    return _create


@pytest.fixture
def mock_k8s_scale_success(mock_k8s_api, mock_k8s_deployment):
    """Mock successful K8s scaling operation.

    Usage:
        scale = mock_k8s_scale_success()
        api = scale["api"]
        scale["api"].read_namespaced_deployment(...)  # Returns mock deployment
    """

    def _create(
        current_replicas: int = 3,
        target_replicas: int = 5,
    ) -> dict:
        """Setup mocks for successful scale operation."""

        api = mock_k8s_api()
        deploy_before = mock_k8s_deployment(current_replicas=current_replicas)
        deploy_after = mock_k8s_deployment(current_replicas=target_replicas)

        # First call returns old state
        api.read_namespaced_deployment.return_value = deploy_before

        # Patch succeeds
        api.patch_namespaced_deployment.return_value = deploy_after

        return {
            "api": api,
            "current_replicas": current_replicas,
            "target_replicas": target_replicas,
            "deploy_before": deploy_before,
            "deploy_after": deploy_after,
        }

    return _create


@pytest.fixture
def mock_k8s_scale_conflict(mock_k8s_api, mock_k8s_deployment):
    """Mock K8s scaling with conflict error (optimistic concurrency).

    Usage:
        conflict = mock_k8s_scale_conflict()
        api = conflict["api"]  # read succeeds, patch raises conflict
    """

    def _create() -> dict:
        """Setup mocks for conflict scenario."""
        from kubernetes.client.exceptions import ApiException

        api = mock_k8s_api()
        deploy = mock_k8s_deployment()

        api.read_namespaced_deployment.return_value = deploy
        api.patch_namespaced_deployment.side_effect = ApiException(409, "Conflict")

        return {
            "api": api,
            "deploy": deploy,
            "error_code": 409,
        }

    return _create


@pytest.fixture
def mock_k8s_scale_timeout(mock_k8s_api):
    """Mock K8s scaling with timeout error.

    Usage:
        timeout = mock_k8s_scale_timeout()
        api = timeout["api"]  # read raises timeout
    """

    def _create() -> dict:
        """Setup mocks for timeout scenario."""
        from kubernetes.client.exceptions import ApiException

        api = mock_k8s_api()
        api.read_namespaced_deployment.side_effect = ApiException(504, "Timeout")

        return {
            "api": api,
            "error_code": 504,
        }

    return _create


@pytest.fixture
def mock_k8s_scale_permission_denied(mock_k8s_api):
    """Mock K8s scaling with permission denied error.

    Usage:
        denied = mock_k8s_scale_permission_denied()
        api = denied["api"]  # operations raise 403
    """

    def _create() -> dict:
        """Setup mocks for permission denied scenario."""
        from kubernetes.client.exceptions import ApiException

        api = mock_k8s_api()
        api.patch_namespaced_deployment.side_effect = ApiException(403, "Forbidden")

        return {
            "api": api,
            "error_code": 403,
        }

    return _create


@pytest.fixture
def mock_kopf():
    """Mock Kopf framework objects.

    Usage:
        kopf_mocks = mock_kopf()
        kopf_mocks["logger"].info("test message")
        kopf_mocks["patch"](...)
    """

    def _create() -> dict:
        """Build dict of Kopf mocks."""
        return {
            "logger": MagicMock(),
            "patch": MagicMock(),
            "status": MagicMock(),
            "exception": MagicMock(),
        }

    return _create


@pytest.fixture
def mock_cr_spec():
    """Mock PredictiveAutoscaler CRD spec.

    Usage:
        spec = mock_cr_spec()
        spec = mock_cr_spec(target_app="my-app", model_path="/custom/model.tflite")
    """

    def _create(
        target_app: str = "test-app",
        target_namespace: str = "default",
        min_replicas: int = 1,
        max_replicas: int = 10,
        model_path: str = None,
        scaler_path: str = None,
        prometheus_url: str = "http://prometheus:9090",
        check_interval_s: int = 30,
    ) -> dict:
        """Build mock CR spec."""

        if model_path is None:
            model_path = f"/models/{target_app}/ppa_model.tflite"
        if scaler_path is None:
            scaler_path = f"/models/{target_app}/scaler.pkl"

        return {
            "targetApp": target_app,
            "targetNamespace": target_namespace,
            "containerName": target_app,
            "minReplicas": min_replicas,
            "maxReplicas": max_replicas,
            "modelPath": model_path,
            "scalerPath": scaler_path,
            "checkIntervalSeconds": check_interval_s,
            "prometheusUrl": prometheus_url,
        }

    return _create


@pytest.fixture
def mock_cr_object(mock_cr_spec):
    """Mock complete PredictiveAutoscaler CR object.

    Usage:
        cr = mock_cr_object()
        cr["metadata"]["name"] = "my-app"
        cr["spec"]["maxReplicas"] = 20
    """

    def _create(name: str = "test-app", namespace: str = "default", **spec_overrides) -> dict:
        """Build mock CR with metadata and spec."""

        spec = mock_cr_spec(**spec_overrides)

        return {
            "apiVersion": "predictiveautoscaler.io/v1",
            "kind": "PredictiveAutoscaler",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "uid": f"uid-{name}",
                "labels": {"app": spec["targetApp"]},
            },
            "spec": spec,
            "status": {
                "currentReplicas": spec["minReplicas"],
                "desiredReplicas": spec["minReplicas"],
                "lastUpdateTime": "2026-04-07T00:00:00Z",
                "historySnapshot": None,
            },
        }

    return _create
