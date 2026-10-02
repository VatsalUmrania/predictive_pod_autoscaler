"""
Kubernetes API and Prometheus Client Helpers
"""

from __future__ import annotations

import logging
import os

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

logger = logging.getLogger(__name__)

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")


def _get_api() -> k8s_client.CoreV1Api:
    try:
        k8s_config.load_incluster_config()
    except Exception:
        k8s_config.load_kube_config()
    return k8s_client.CoreV1Api()


def _get_apps_api() -> k8s_client.AppsV1Api:
    try:
        k8s_config.load_incluster_config()
    except Exception:
        k8s_config.load_kube_config()
    return k8s_client.AppsV1Api()
