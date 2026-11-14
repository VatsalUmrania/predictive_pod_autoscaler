"""
Kubernetes Mutating Tools (Restart, Patch, Scale, Cordon, Drain)
"""

from __future__ import annotations

import datetime
import logging

from kubernetes import client as k8s_client

from nexus.agents.k8s_tools.client import _get_api, _get_apps_api

logger = logging.getLogger(__name__)


def restart_deployment(namespace: str, deployment_name: str) -> str:
    """Perform a rolling restart of a deployment."""
    api = _get_apps_api()
    try:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat("T") + "Z"
        body = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {"kubectl.kubernetes.io/restartedAt": now}
                    }
                }
            }
        }
        api.patch_namespaced_deployment(
            name=deployment_name, namespace=namespace, body=body
        )
        return f"Successfully initiated restart for deployment {deployment_name}."
    except Exception as e:
        return f"Error restarting deployment: {str(e)}"


def patch_configmap(
    namespace: str, configmap_name: str, patch_data: dict[str, str]
) -> str:
    """Patch a ConfigMap with new key/value pairs."""
    api = _get_api()
    try:
        body = {"data": patch_data}
        api.patch_namespaced_config_map(
            name=configmap_name, namespace=namespace, body=body
        )
        return f"Successfully patched configmap {configmap_name}."
    except Exception as e:
        return f"Error patching configmap: {str(e)}"


def scale_resource(
    namespace: str, resource_kind: str, resource_name: str, replicas: int
) -> str:
    """Scale a Deployment, StatefulSet, or ReplicaSet to the specified replicas."""
    if resource_kind.lower() == "deployment":
        api = _get_apps_api()
        try:
            body = {"spec": {"replicas": replicas}}
            api.patch_namespaced_deployment_scale(
                name=resource_name, namespace=namespace, body=body
            )
            return f"Scaled deployment {resource_name} to {replicas}."
        except Exception as e:
            return f"Error scaling deployment: {str(e)}"
    return f"Scaling for {resource_kind} not supported yet."


def cordon_node(node_name: str) -> str:
    """Mark a node as unschedulable."""
    api = _get_api()
    try:
        body = {"spec": {"unschedulable": True}}
        api.patch_node(name=node_name, body=body)
        return f"Node {node_name} cordoned."
    except Exception as e:
        return f"Error cordoning node: {str(e)}"


def drain_node(node_name: str) -> str:
    """Drain a node: cordon + evict all pods (respecting PDBs)."""
    api = _get_api()
    try:
        api.patch_node(name=node_name, body={"spec": {"unschedulable": True}})

        pods = api.list_pod_for_all_namespaces(
            field_selector=f"spec.nodeName={node_name}"
        ).items

        evicted = 0
        failed = 0
        for pod in pods:
            ns = pod.metadata.namespace
            pname = pod.metadata.name
            owner_refs = pod.metadata.owner_references or []
            if any(ref.kind == "DaemonSet" for ref in owner_refs):
                continue

            eviction = k8s_client.V1Eviction(
                metadata=k8s_client.V1ObjectMeta(name=pname, namespace=ns),
                delete_options=k8s_client.V1DeleteOptions(),
            )
            try:
                api.create_namespaced_pod_eviction(
                    name=pname, namespace=ns, body=eviction
                )
                evicted += 1
            except Exception as e:
                failed += 1
                logger.warning(f"Failed to evict {ns}/{pname}: {e}")

        return f"Node {node_name} drained: cordoned, evicted {evicted} pod(s), {failed} failed."

    except Exception as e:
        return f"Error draining node {node_name}: {str(e)}"
