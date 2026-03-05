# operator/scaler.py — replica calculation + K8s scaling
"""Calculates desired replicas and patches deployments."""

import logging
import math
from kubernetes import client, config as k8s_config

logger = logging.getLogger("ppa.scaler")

try:
    k8s_config.load_incluster_config()
except k8s_config.ConfigException:
    k8s_config.load_kube_config()

apps_v1 = client.AppsV1Api()


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
