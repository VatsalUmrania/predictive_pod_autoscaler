# operator/scaler.py — Replica calculation + Kubernetes scaling
"""Calculate desired replicas and apply via kubernetes-client."""

import math
import logging
from kubernetes import client, config as k8s_config

from config import (
    NAMESPACE, CAPACITY_PER_POD,
    MIN_REPLICAS, MAX_REPLICAS,
    SCALE_UP_RATE_LIMIT, SCALE_DOWN_RATE,
)

logger = logging.getLogger("ppa.scaler")


def calculate_replicas(predicted_load: float, current_replicas: int) -> int:
    """Convert predicted load (req/s) to desired replica count with rate limiting.

    Args:
        predicted_load: Predicted requests per second.
        current_replicas: Current number of running replicas.

    Returns:
        Desired replica count, clamped to [MIN, MAX] and rate-limited.
    """
    if predicted_load <= 0:
        return MIN_REPLICAS

    raw_desired = math.ceil(predicted_load / CAPACITY_PER_POD)

    # Rate-limit scaling
    max_up = math.ceil(current_replicas * SCALE_UP_RATE_LIMIT)
    min_down = max(MIN_REPLICAS, math.floor(current_replicas * SCALE_DOWN_RATE))

    if raw_desired > current_replicas:
        desired = min(raw_desired, max_up)
    elif raw_desired < current_replicas:
        desired = max(raw_desired, min_down)
    else:
        desired = current_replicas

    return max(MIN_REPLICAS, min(MAX_REPLICAS, desired))


def scale_deployment(deployment: str, replicas: int, namespace: str = NAMESPACE):
    """Patch deployment replicas via Kubernetes API.

    Args:
        deployment: Name of the Deployment to scale.
        replicas: Desired replica count.
        namespace: Kubernetes namespace.
    """
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()  # local dev fallback

    apps_v1 = client.AppsV1Api()
    body = {"spec": {"replicas": replicas}}
    apps_v1.patch_namespaced_deployment_scale(
        name=deployment,
        namespace=namespace,
        body=body,
    )
    logger.info(f"Scaled {deployment} to {replicas} replicas")
