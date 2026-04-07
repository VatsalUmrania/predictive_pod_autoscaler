"""Kubernetes API client adapter.

This module provides low-level K8s API interactions:
- Client initialization with automatic retry and context detection
- Deployment scaling via patch operations
- Lazy initialization and caching for performance

Thread-safe and resilient to transient failures (network timeouts, auth issues).
"""

import logging
import time

from kubernetes import client
from kubernetes import config as k8s_config

logger = logging.getLogger("ppa.infrastructure.kubernetes")

# Module-level cache for K8s API client (lazy-initialized)
_apps_v1_instance: client.AppsV1Api | None = None


def init_k8s_client(retries: int = 3, backoff: float = 5.0) -> client.AppsV1Api:
    """Initialize K8s API client with retry on transient failures.

    Attempts to load K8s config in this order:
    1. In-cluster config (Pod service account)
    2. User kubeconfig (~/.kube/config)

    Args:
        retries: Number of initialization attempts before failing
        backoff: Sleep duration between retry attempts (seconds)

    Returns:
        Initialized AppsV1Api client ready for deployment operations

    Raises:
        RuntimeError: If all initialization attempts fail
        ConfigException: If neither in-cluster nor kubeconfig auth available

    Example:
        >>> api = init_k8s_client(retries=3, backoff=5.0)
        >>> print("K8s client ready")
    """
    for attempt in range(1, retries + 1):
        try:
            k8s_config.load_incluster_config()
            logger.info("K8s config loaded from in-cluster service account")
            return client.AppsV1Api()
        except k8s_config.ConfigException:
            try:
                k8s_config.load_kube_config()
                logger.info("K8s config loaded from user kubeconfig")
                return client.AppsV1Api()
            except Exception as exc:
                if attempt < retries:
                    logger.warning(
                        f"K8s client init attempt {attempt}/{retries} failed: {exc}, "
                        f"retrying in {backoff}s..."
                    )
                    time.sleep(backoff)
                else:
                    logger.error(
                        f"K8s client init failed after {retries} attempts: {exc}"
                    )
                    raise
    raise RuntimeError("K8s client init exhausted retries")  # unreachable


def get_apps_v1(retries: int = 3, backoff: float = 5.0) -> client.AppsV1Api | None:
    """Lazy-initialize and cache K8s API client. Returns None on persistent failure.

    On first call, initializes the K8s API client and caches it globally.
    Subsequent calls return the cached instance.

    Designed for resilience: if initialization fails, returns None rather than
    raising an exception. Caller can retry on next cycle.

    Args:
        retries: Initialization retry attempts (only used on first call)
        backoff: Retry backoff duration in seconds

    Returns:
        Initialized AppsV1Api client, or None if initialization failed

    Example:
        >>> api = get_apps_v1()
        >>> if api is None:
        ...     print("K8s unavailable, will retry next cycle")
        ... else:
        ...     print("K8s API ready")
    """
    global _apps_v1_instance

    if _apps_v1_instance is not None:
        return _apps_v1_instance

    try:
        _apps_v1_instance = init_k8s_client(retries=retries, backoff=backoff)
        logger.info("K8s API client initialized successfully")
        return _apps_v1_instance
    except Exception as exc:
        logger.error(f"Failed to initialize K8s client: {exc}")
        return None


def scale_deployment(deployment: str, replicas: int, namespace: str = "default") -> bool:
    """Patch the Deployment's replica count.

    Atomically updates a Deployment's desired replica count via K8s API patch.
    Fails gracefully on transient errors (logs warning, doesn't crash).

    Args:
        deployment: Deployment name
        replicas: Desired replica count (must be >= 0)
        namespace: Kubernetes namespace (default: "default")

    Returns:
        True if patch succeeded, False if K8s API unavailable

    Example:
        >>> success = scale_deployment("my-api", replicas=5, namespace="production")
        >>> if success:
        ...     print("Scaling applied")
        ... else:
        ...     print("K8s unavailable, retry next cycle")
    """
    api = get_apps_v1()

    if api is None:
        logger.error(
            f"Cannot scale {namespace}/{deployment}: K8s API client unavailable. "
            f"Will retry on next reconciliation cycle."
        )
        return False

    try:
        api.patch_namespaced_deployment_scale(
            name=deployment,
            namespace=namespace,
            body={"spec": {"replicas": replicas}},
        )
        logger.info(f"Patched {namespace}/{deployment} to {replicas} replicas")
        return True
    except client.exceptions.ApiException as e:
        logger.error(f"Failed to scale {namespace}/{deployment}: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error scaling {namespace}/{deployment}: {e}")
        return False


__all__ = [
    "init_k8s_client",
    "get_apps_v1",
    "scale_deployment",
]
