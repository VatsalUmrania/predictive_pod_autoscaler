"""
Kubernetes Tools for LLM Orchestrator
=====================================
Provides read and write functions that the LLM agent can call
to diagnose and remediate cluster issues.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

logger = logging.getLogger(__name__)

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

# Read Tools

def get_pod_logs(namespace: str, pod_name: str, tail_lines: int = 50) -> str:
    """Fetch logs from a specific pod."""
    api = _get_api()
    try:
        logs = api.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            tail_lines=tail_lines
        )
        return logs
    except Exception as e:
        return f"Error fetching logs for {pod_name}: {str(e)}"

def describe_resource(namespace: str, resource_kind: str, resource_name: str) -> str:
    """Get description/status of a Kubernetes resource."""
    # Simplified description, in a real implementation this would fetch the object and format it
    try:
        if resource_kind.lower() == "pod":
            api = _get_api()
            obj = api.read_namespaced_pod(name=resource_name, namespace=namespace)
            return str(obj.status)
        elif resource_kind.lower() == "deployment":
            api = _get_apps_api()
            obj = api.read_namespaced_deployment(name=resource_name, namespace=namespace)
            return str(obj.status)
        return f"Describe not fully implemented for {resource_kind}"
    except Exception as e:
        return f"Error describing {resource_kind} {resource_name}: {str(e)}"

def get_events(namespace: str, resource_name: str = None) -> str:
    """Get recent events in a namespace, optionally filtered by resource."""
    api = _get_api()
    try:
        events = api.list_namespaced_event(namespace=namespace)
        relevant_events = []
        for event in events.items:
            if not resource_name or (event.involved_object and event.involved_object.name == resource_name):
                relevant_events.append(f"{event.type}: {event.message} ({event.reason})")
        return "\n".join(relevant_events) if relevant_events else "No recent events found."
    except Exception as e:
        return f"Error fetching events: {str(e)}"

def get_metrics(namespace: str, pod_name: str) -> str:
    """Fetch basic metrics for a pod (CPU/Memory)."""
    # Placeholder for metric fetching (e.g. from metrics-server or prometheus)
    return f"Metrics for {pod_name} in {namespace}: CPU 150m, Memory 256Mi (Simulated)"

def get_yaml(namespace: str, resource_kind: str, resource_name: str) -> str:
    """Fetch the YAML definition of a resource."""
    # Placeholder for YAML fetching
    return f"YAML for {resource_kind} {resource_name} in {namespace} (Simulated)"

# Write Tools 

def restart_deployment(namespace: str, deployment_name: str) -> str:
    """Perform a rolling restart of a deployment."""
    api = _get_apps_api()
    try:
        import datetime
        now = datetime.datetime.utcnow().isoformat("T") + "Z"
        body = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": now
                        }
                    }
                }
            }
        }
        api.patch_namespaced_deployment(name=deployment_name, namespace=namespace, body=body)
        return f"Successfully initiated restart for deployment {deployment_name}."
    except Exception as e:
        return f"Error restarting deployment: {str(e)}"

def rollback_deployment(namespace: str, deployment_name: str) -> str:
    """Roll back a deployment to its previous ReplicaSet revision.

    Kubernetes Python client 30+ dropped the typed rollback subresource, so we
    undo manually: find the second-newest ReplicaSet (the last good revision)
    and patch the deployment's pod template to match it — equivalent to
    `kubectl rollout undo`. Mirrors runbook_executor's kubectl_rollout_undo.
    """
    api = _get_apps_api()
    try:
        rs_list = api.list_namespaced_replica_set(
            namespace, label_selector=f"app={deployment_name}"
        ).items
        sorted_rs = sorted(
            rs_list,
            key=lambda r: r.metadata.creation_timestamp or 0,
            reverse=True,
        )
        if len(sorted_rs) < 2:
            return f"No previous ReplicaSet found for {namespace}/{deployment_name}"
        prev_template = sorted_rs[1].spec.template
        patch = {"spec": {"template": prev_template.to_dict()}}
        api.patch_namespaced_deployment(name=deployment_name, namespace=namespace, body=patch)
        return (
            f"Rolled back {namespace}/{deployment_name} to revision on "
            f"ReplicaSet {sorted_rs[1].metadata.name}"
        )
    except Exception as e:
        return f"Error rolling back deployment: {str(e)}"

def patch_configmap(namespace: str, configmap_name: str, patch_data: Dict[str, str]) -> str:
    """Patch a ConfigMap with new key/value pairs."""
    api = _get_api()
    try:
        body = {"data": patch_data}
        api.patch_namespaced_config_map(name=configmap_name, namespace=namespace, body=body)
        return f"Successfully patched configmap {configmap_name}."
    except Exception as e:
        return f"Error patching configmap: {str(e)}"

def scale_resource(namespace: str, resource_kind: str, resource_name: str, replicas: int) -> str:
    """Scale a Deployment, StatefulSet, or ReplicaSet to the specified replicas."""
    if resource_kind.lower() == "deployment":
        api = _get_apps_api()
        try:
            body = {"spec": {"replicas": replicas}}
            api.patch_namespaced_deployment_scale(name=resource_name, namespace=namespace, body=body)
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
    """Drain a node in preparation for maintenance."""
    return f"Node {node_name} draining initiated. (Simulated)"
