"""
Kubernetes Tools for LLM Orchestrator
=====================================
Provides read and write functions that the LLM agent can call
to diagnose and remediate cluster issues.
"""

from __future__ import annotations

import logging
import os

import httpx
import yaml
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

logger = logging.getLogger(__name__)

# Prometheus URL for metrics queries (fallback to common in-cluster default)
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
    """Fetch real CPU/Memory metrics for a pod from Prometheus.

    Queries container_cpu_usage_seconds_total and container_memory_working_set_bytes
    via PromQL, returns human-readable string. Falls back to simulated if Prometheus
    is unavailable.
    """
    try:
        # Query for CPU (rate over 1m) and current memory
        cpu_query = f'sum(rate(container_cpu_usage_seconds_total{{namespace="{namespace}",pod="{pod_name}",container!=""}}[1m])) by (pod)'
        mem_query = f'sum(container_memory_working_set_bytes{{namespace="{namespace}",pod="{pod_name}",container!=""}}) by (pod)'

        cpu_val = None
        mem_val = None

        # Blocking httpx calls (tool functions are sync)
        with httpx.Client(timeout=5.0) as client:
            # CPU
            resp = client.get(
                f"{PROMETHEUS_URL}/api/v1/query",
                params={"query": cpu_query}
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {}).get("result", [])
                if data:
                    cpu_val = float(data[0]["value"][1])

            # Memory
            resp = client.get(
                f"{PROMETHEUS_URL}/api/v1/query",
                params={"query": mem_query}
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {}).get("result", [])
                if data:
                    mem_val = float(data[0]["value"][1])

        if cpu_val is not None and mem_val is not None:
            # Convert: CPU cores -> millicores, memory bytes -> Mi
            cpu_m = cpu_val * 1000
            mem_mi = mem_val / (1024 * 1024)
            return f"Metrics for {pod_name} in {namespace}: CPU {cpu_m:.0f}m, Memory {mem_mi:.0f}Mi"
        elif cpu_val is not None:
            cpu_m = cpu_val * 1000
            return f"Metrics for {pod_name} in {namespace}: CPU {cpu_m:.0f}m, Memory unavailable"
        elif mem_val is not None:
            mem_mi = mem_val / (1024 * 1024)
            return f"Metrics for {pod_name} in {namespace}: CPU unavailable, Memory {mem_mi:.0f}Mi"
        else:
            return f"Metrics for {pod_name} in {namespace}: no data from Prometheus"

    except Exception as e:
        logger.warning(f"get_metrics failed for {pod_name}: {e}")
        return f"Metrics for {pod_name} in {namespace}: error fetching ({e})"

def get_yaml(namespace: str, resource_kind: str, resource_name: str) -> str:
    """Fetch the actual YAML definition of a resource from the kube API."""
    api = _get_api()
    try:
        kind = resource_kind.lower()
        if kind == "pod":
            obj = api.read_namespaced_pod(name=resource_name, namespace=namespace)
        elif kind == "deployment":
            api = _get_apps_api()
            obj = api.read_namespaced_deployment(name=resource_name, namespace=namespace)
        elif kind == "service":
            obj = api.read_namespaced_service(name=resource_name, namespace=namespace)
        elif kind == "configmap":
            obj = api.read_namespaced_config_map(name=resource_name, namespace=namespace)
        elif kind == "secret":
            obj = api.read_namespaced_secret(name=resource_name, namespace=namespace)
        elif kind == "statefulset":
            api = _get_apps_api()
            obj = api.read_namespaced_stateful_set(name=resource_name, namespace=namespace)
        elif kind == "daemonset":
            api = _get_apps_api()
            obj = api.read_namespaced_daemon_set(name=resource_name, namespace=namespace)
        else:
            return f"YAML fetch not implemented for {resource_kind}"

        # Convert to dict and dump as YAML (strip managed fields)
        data = obj.to_dict()
        for key in ["status", "managed_fields", "creation_timestamp", "resource_version", "uid"]:
            data.pop(key, None)
        if "metadata" in data:
            for key in ["creation_timestamp", "resource_version", "uid", "generation"]:
                data["metadata"].pop(key, None)
        return yaml.dump(data, default_flow_style=False)

    except Exception as e:
        return f"Error fetching YAML for {resource_kind} {resource_name}: {str(e)}"

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

def patch_configmap(namespace: str, configmap_name: str, patch_data: dict[str, str]) -> str:
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
    """Drain a node: cordon + evict all pods (respecting PDBs)."""
    api = _get_api()
    try:
        # First cordon
        api.patch_node(name=node_name, body={"spec": {"unschedulable": True}})

        # List pods on the node
        pods = api.list_pod_for_all_namespaces(
            field_selector=f"spec.nodeName={node_name}"
        ).items

        # Evict each pod using the eviction/v1 API
        evicted = 0
        failed = 0
        for pod in pods:
            ns = pod.metadata.namespace
            pname = pod.metadata.name
            # Skip DaemonSet pods (they'll be rescheduled immediately)
            owner_refs = pod.metadata.owner_references or []
            if any(ref.kind == "DaemonSet" for ref in owner_refs):
                continue

            eviction = k8s_client.V1Eviction(
                metadata=k8s_client.V1ObjectMeta(name=pname, namespace=ns),
                delete_options=k8s_client.V1DeleteOptions(),
            )
            try:
                api.create_namespaced_pod_eviction(name=pname, namespace=ns, body=eviction)
                evicted += 1
            except Exception as e:
                failed += 1
                logger.warning(f"Failed to evict {ns}/{pname}: {e}")

        return f"Node {node_name} drained: cordoned, evicted {evicted} pod(s), {failed} failed."

    except Exception as e:
        return f"Error draining node {node_name}: {str(e)}"
