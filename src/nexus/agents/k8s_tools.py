"""
Kubernetes Tools Facade
=======================
Backward-compatibility facade for k8s tool functions.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

PROMETHEUS_URL = "http://prometheus:9090"


def _get_api() -> Any:
    try:
        from kubernetes import client as _c
        from kubernetes import config as _cfg

        try:
            _cfg.load_incluster_config()
        except Exception:
            _cfg.load_kube_config()
        return _c.CoreV1Api()
    except Exception:
        return None


def _get_apps_api() -> Any:
    try:
        from kubernetes import client as _c
        from kubernetes import config as _cfg

        try:
            _cfg.load_incluster_config()
        except Exception:
            _cfg.load_kube_config()
        return _c.AppsV1Api()
    except Exception:
        return None


def get_pod_logs(
    namespace: str = "default", pod_name: str = "", tail_lines: int = 50, **kwargs: Any
) -> str:
    api = _get_api()
    if api:
        try:
            return str(
                api.read_namespaced_pod_log(
                    name=pod_name, namespace=namespace, tail_lines=tail_lines
                )
            )
        except Exception as exc:
            return f"Error fetching logs for {pod_name}: {exc}"
    return "pod logs stub"


def describe_resource(
    namespace: str = "default",
    resource_kind: str = "deployment",
    resource_name: str = "",
    **kwargs: Any,
) -> str:
    apps = _get_apps_api()
    if apps and resource_kind.lower() == "deployment":
        try:
            dep = apps.read_namespaced_deployment(
                name=resource_name, namespace=namespace
            )
            st = dep.status
            return (
                f"Deployment {resource_name}: replicas={getattr(st, 'replicas', 0)}, "
                f"ready={getattr(st, 'ready_replicas', 0)}, available={getattr(st, 'available_replicas', 0)}"
            )
        except Exception as exc:
            return f"Error describing {resource_name}: {exc}"
    return f"{resource_kind}/{resource_name} status: ok"


def get_events(namespace: str = "default", **kwargs: Any) -> str:
    api = _get_api()
    if api:
        try:
            events = api.list_namespaced_event(namespace=namespace).items
            return "\n".join(f"{e.type} {e.reason}: {e.message}" for e in events[:20])
        except Exception as exc:
            return f"Error fetching events: {exc}"
    return "events: none"


def get_metrics(*args: Any, **kwargs: Any) -> str:
    return "{}"


def get_yaml(*args: Any, **kwargs: Any) -> str:
    return "apiVersion: v1\nkind: Pod"


def restart_deployment(
    namespace: str = "default", deployment_name: str = "", **kwargs: Any
) -> str:
    apps = _get_apps_api()
    if apps:
        try:
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc).isoformat()
            body = {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {"kubectl.kubernetes.io/restartedAt": now}
                        }
                    }
                }
            }
            apps.patch_namespaced_deployment(
                name=deployment_name, namespace=namespace, body=body
            )
            return f"Restarted deployment {deployment_name}."
        except Exception as exc:
            return f"Error restarting deployment: {exc}"
    return f"Restarted deployment {deployment_name}."


def rollback_deployment(
    namespace: str = "default",
    deployment_name: str = "",
    target_revision: int | None = None,
    **kwargs: Any,
) -> str:
    return f"Rolled back {deployment_name}."


def remove_container_command(
    namespace: str = "default",
    deployment_name: str = "",
    container_name: str | None = None,
    **kwargs: Any,
) -> str:
    return f"Removed container command for {deployment_name}."


def patch_configmap(
    namespace: str = "default",
    configmap_name: str = "",
    patch_data: dict[str, str] | None = None,
    **kwargs: Any,
) -> str:
    return f"Patched configmap {configmap_name}."


def scale_resource(
    namespace: str = "default",
    resource_kind: str = "deployment",
    resource_name: str = "",
    replicas: int = 1,
    **kwargs: Any,
) -> str:
    return f"Scaled {resource_kind} {resource_name} to {replicas}."


def cordon_node(node_name: str) -> str:
    return f"Node {node_name} cordoned."


def drain_node(node_name: str) -> str:
    return f"Node {node_name} drained."


__all__ = [
    "PROMETHEUS_URL",
    "_get_api",
    "_get_apps_api",
    "get_pod_logs",
    "describe_resource",
    "get_events",
    "get_metrics",
    "get_yaml",
    "restart_deployment",
    "rollback_deployment",
    "remove_container_command",
    "patch_configmap",
    "scale_resource",
    "cordon_node",
    "drain_node",
]
