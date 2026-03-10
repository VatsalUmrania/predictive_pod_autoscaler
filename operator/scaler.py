# operator/scaler.py — replica calculation + K8s scaling
"""Calculates desired replicas and patches deployments."""

import logging
import math
import time
from kubernetes import client, config as k8s_config

logger = logging.getLogger("ppa.scaler")


def _init_k8s_client(retries: int = 3, backoff: float = 5.0) -> client.AppsV1Api:
    """Initialize K8s API client with retry on transient failures."""
    for attempt in range(1, retries + 1):
        try:
            k8s_config.load_incluster_config()
            return client.AppsV1Api()
        except k8s_config.ConfigException:
            try:
                k8s_config.load_kube_config()
                return client.AppsV1Api()
            except Exception as exc:
                if attempt < retries:
                    logger.warning(
                        f"K8s client init attempt {attempt}/{retries} failed: {exc}, "
                        f"retrying in {backoff}s..."
                    )
                    time.sleep(backoff)
                else:
                    logger.error(f"K8s client init failed after {retries} attempts: {exc}")
                    raise
    raise RuntimeError("K8s client init exhausted retries")  # unreachable


apps_v1 = _init_k8s_client()


def calculate_replicas(
    predicted_load: float,
    current: int,
    min_replicas: int,
    max_replicas: int,
    capacity_per_pod: int,
    scale_up_rate: float,
    scale_down_rate: float,
) -> int:
    """Compute desired replica count from predicted load with rate limiting."""
    raw = math.ceil(predicted_load / capacity_per_pod) if capacity_per_pod > 0 else current

    # Rate limiting
    max_up = max(1, math.ceil(current * scale_up_rate))
    min_down = max(1, math.floor(current * scale_down_rate))
    desired = max(min_down, min(max_up, raw))

    # Enforce hard bounds
    return max(min_replicas, min(max_replicas, desired))


def scale_deployment(deployment: str, replicas: int, namespace: str = "default"):
    """Patch the Deployment's replica count."""
    try:
        apps_v1.patch_namespaced_deployment_scale(
            name=deployment,
            namespace=namespace,
            body={"spec": {"replicas": replicas}},
        )
        logger.info(f"Patched {namespace}/{deployment} to {replicas} replicas")
    except client.exceptions.ApiException as e:
        logger.error(f"Failed to scale {namespace}/{deployment}: {e}")
