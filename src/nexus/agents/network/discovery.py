"""
Kubernetes Service Hostname Discovery
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def discover_k8s_service_hostnames(namespaces: list[str] | None = None) -> list[str]:
    """
    Return a list of DNS names for all Kubernetes Services in the given namespaces.
    Falls back to empty list if K8s API is unreachable.

    DNS format: <service>.<namespace>.svc.cluster.local
    """
    hostnames: list[str] = []
    try:
        from kubernetes import client as k8s_client
        from kubernetes import config as k8s_config

        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()

        v1 = k8s_client.CoreV1Api()

        if namespaces:
            services = []
            for ns in namespaces:
                services.extend(v1.list_namespaced_service(ns).items)
        else:
            services = v1.list_service_for_all_namespaces().items

        for svc in services:
            name = svc.metadata.name
            ns = svc.metadata.namespace
            if ns in ("kube-system", "kube-public", "kube-node-lease"):
                continue
            if svc.spec.cluster_ip == "None":
                continue
            hostnames.append(f"{name}.{ns}.svc.cluster.local")

    except Exception as exc:
        logger.debug(f"[NetworkAgent] K8s service discovery unavailable: {exc}")

    return hostnames
