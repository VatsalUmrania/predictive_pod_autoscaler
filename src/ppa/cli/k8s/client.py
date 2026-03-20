"""Kubernetes client helpers."""

from kubernetes import client, config

_kube_config: client.ApiClient | None = None


def get_client() -> client.ApiClient:
    """Get or create Kubernetes API client."""
    global _kube_config
    if _kube_config is None:
        config.load_kube_config()
        _kube_config = config.new_client_from_config()
    return _kube_config


def get_core_v1() -> client.CoreV1Api:
    """Get CoreV1 API."""
    return client.CoreV1Api(get_client())


def get_apps_v1() -> client.AppsV1Api:
    """Get AppsV1 API."""
    return client.AppsV1Api(get_client())
