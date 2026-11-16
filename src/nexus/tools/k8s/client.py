"""
Kubernetes API Client Factory
=============================
Instantiates and authenticates Kubernetes API client instances.
Mirrors nexus.tools.aws.client.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def get_k8s_client(api_cls: str = "CoreV1Api") -> Any:
    """Instantiate and authenticate a Kubernetes API client instance."""
    try:
        from kubernetes import client as _c
        from kubernetes import config as _cfg

        try:
            _cfg.load_incluster_config()
        except Exception:
            _cfg.load_kube_config()
        conf = _c.Configuration.get_default_copy()
        conf.retries = 1
        api_client = _c.ApiClient(configuration=conf)
        return getattr(_c, api_cls)(api_client=api_client)
    except Exception as exc:
        logger.debug("[K8sClient] Could not load client %s: %s", api_cls, exc)
        return None
