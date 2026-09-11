"""
Kubernetes Tools for LLM Orchestrator
=====================================
Provides read and write functions that the LLM agent can call
to diagnose and remediate cluster issues.
"""

from __future__ import annotations

import logging
import os
from typing import Any

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
    """Fetch logs from a specific pod, with fallback to deployment pods."""
    api = _get_api()
    try:
        logs = api.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            tail_lines=tail_lines,
        )
        return str(logs)
    except Exception as e:
        try:
            pods = api.list_namespaced_pod(namespace=namespace).items
            matching = [
                p.metadata.name for p in pods
                if p.metadata.name.startswith(f"{pod_name}-")
                or (p.metadata.labels and p.metadata.labels.get("app") == pod_name)
            ]
            if matching:
                return str(api.read_namespaced_pod_log(
                    name=matching[0],
                    namespace=namespace,
                    tail_lines=tail_lines,
                ))
        except Exception:
            pass
        return f"Error fetching logs for {pod_name}: {str(e)}"

def describe_resource(namespace: str, resource_kind: str, resource_name: str) -> str:
    """Get description/status of a Kubernetes resource."""
    try:
        if resource_kind.lower() == "pod":
            api = _get_api()
            obj = api.read_namespaced_pod(name=resource_name, namespace=namespace)
            return str(obj.status)
        elif resource_kind.lower() == "deployment":
            api = _get_apps_api()
            core_api = _get_api()
            obj = api.read_namespaced_deployment(name=resource_name, namespace=namespace)
            st = obj.status
            lines = [
                f"Deployment {resource_name}: replicas={getattr(st, 'replicas', 0)}, "
                f"updated={getattr(st, 'updated_replicas', 0)}, "
                f"ready={getattr(st, 'ready_replicas', 0)}, "
                f"available={getattr(st, 'available_replicas', 0)}, "
                f"unavailable={getattr(st, 'unavailable_replicas', 0)}"
            ]

            # Container specifications (image, command, args)
            if obj.spec and obj.spec.template and obj.spec.template.spec and obj.spec.template.spec.containers:
                for c in obj.spec.template.spec.containers:
                    lines.append(f"Container Spec [{c.name}]: image={c.image}, command={c.command}, args={c.args}")

            # ReplicaSet rollout inspection
            try:
                rs_list = api.list_namespaced_replica_set(namespace=namespace).items
                matching_rs = [
                    rs for rs in rs_list
                    if (rs.metadata.owner_references and any(ref.name == resource_name for ref in rs.metadata.owner_references))
                    or rs.metadata.name.startswith(f"{resource_name}-")
                ]
                active_rs = [rs for rs in matching_rs if (rs.spec.replicas or 0) > 0 or (getattr(rs.status, "replicas", 0) or 0) > 0]
                lines.append(f"ReplicaSets: {len(matching_rs)} total, {len(active_rs)} active (active rollout={len(active_rs) > 1})")
                for rs in active_rs:
                    rev = rs.metadata.annotations.get("deployment.kubernetes.io/revision") if rs.metadata.annotations else None
                    lines.append(f"  RS {rs.metadata.name} (rev={rev}): desired={rs.spec.replicas}, ready={getattr(rs.status, 'ready_replicas', 0)}")
            except Exception:
                pass

            # Pod statuses with termination exit codes
            try:
                pods = core_api.list_namespaced_pod(namespace=namespace).items
                matching_pods = [
                    p for p in pods
                    if p.metadata.name.startswith(f"{resource_name}-")
                    or (p.metadata.labels and p.metadata.labels.get("app") == resource_name)
                ]
                for p in matching_pods:
                    c_info = []
                    for cs in (p.status.container_statuses or []):
                        wait_reason = cs.state.waiting.reason if (cs.state and cs.state.waiting) else None
                        term_state = cs.state.terminated if (cs.state and cs.state.terminated) else (cs.last_state.terminated if cs.last_state else None)
                        term_reason = term_state.reason if term_state else None
                        exit_code = term_state.exit_code if term_state else None
                        c_info.append(
                            f"{cs.name}: restarts={cs.restart_count}, waiting={wait_reason}, "
                            f"terminated={term_reason}, exit_code={exit_code}"
                        )
                    lines.append(f"Pod {p.metadata.name} (phase={p.status.phase}): {', '.join(c_info)}")
            except Exception:
                pass

            # Recent events
            try:
                evts = get_events(namespace=namespace, resource_name=resource_name)
                if evts and "No recent events" not in evts:
                    lines.append(f"Recent Events:\n{evts}")
            except Exception:
                pass

            return "\n".join(lines)
        return f"Describe not fully implemented for {resource_kind}"
    except Exception as e:
        return f"Error describing {resource_kind} {resource_name}: {str(e)}"

def get_events(namespace: str, resource_name: str | None = None) -> str:
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
        return str(yaml.dump(data, default_flow_style=False))

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

def rollback_deployment(
    namespace: str, deployment_name: str, target_revision: int | None = None
) -> str:
    """Roll back a deployment to a previous stable ReplicaSet revision.

    Performs an intelligent rollback:
    1. Reads deployment and identifies matching ReplicaSets by selector.
    2. Sorts ReplicaSets by deployment.kubernetes.io/revision integer descending.
    3. If target_revision is unspecified, skips intermediate ReplicaSets that share
       the identical faulty container command/override to find the true stable revision.
    4. Applies the clean template via replace_namespaced_deployment (or sanitized patch fallback),
       eliminating OpenAPI snake_case merge key errors.
    """
    api = _get_apps_api()
    try:
        dep = api.read_namespaced_deployment(name=deployment_name, namespace=namespace)

        match_labels = dep.spec.selector.match_labels if dep.spec and dep.spec.selector else None
        if match_labels:
            selector = ",".join(f"{k}={v}" for k, v in match_labels.items())
        else:
            selector = f"app={deployment_name}"

        rs_list = api.list_namespaced_replica_set(
            namespace=namespace, label_selector=selector
        ).items

        def _get_rev(r: Any) -> int:
            try:
                ann = r.metadata.annotations or {}
                return int(ann.get("deployment.kubernetes.io/revision", 0))
            except (ValueError, TypeError, AttributeError):
                return 0

        sorted_rs = sorted(rs_list, key=_get_rev, reverse=True)
        if len(sorted_rs) < 2:
            return f"No previous ReplicaSet found for {namespace}/{deployment_name} to roll back to."

        chosen_rs = None
        if target_revision is not None:
            for rs in sorted_rs:
                if _get_rev(rs) == target_revision:
                    chosen_rs = rs
                    break
            if not chosen_rs:
                return f"ReplicaSet revision {target_revision} not found for {namespace}/{deployment_name}"
        else:
            # Smart rollback: search backwards from sorted_rs[1] to skip any revisions
            # that have the exact same crashing container command/args override.
            curr_containers = dep.spec.template.spec.containers if (dep.spec and dep.spec.template and dep.spec.template.spec) else []
            curr_c0 = curr_containers[0] if curr_containers else None
            curr_command = getattr(curr_c0, "command", None)

            for rs in sorted_rs[1:]:
                rs_containers = rs.spec.template.spec.containers if (rs.spec and rs.spec.template and rs.spec.template.spec) else []
                rs_c0 = rs_containers[0] if rs_containers else None
                rs_command = getattr(rs_c0, "command", None)
                if curr_command and rs_command == curr_command:
                    continue
                chosen_rs = rs
                break

            if not chosen_rs:
                chosen_rs = sorted_rs[1]

        target_template = chosen_rs.spec.template
        chosen_rev = _get_rev(chosen_rs)

        # Remove pod-template-hash label from template metadata so k8s controller generates fresh hash
        if target_template.metadata and target_template.metadata.labels:
            target_template.metadata.labels.pop("pod-template-hash", None)

        import datetime
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if not target_template.metadata:
            target_template.metadata = k8s_client.V1ObjectMeta()
        if not target_template.metadata.annotations:
            target_template.metadata.annotations = {}
        target_template.metadata.annotations["kubectl.kubernetes.io/restartedAt"] = now

        dep.spec.template = target_template

        # Apply using typed replace_namespaced_deployment with sanitized patch fallback
        try:
            api.replace_namespaced_deployment(name=deployment_name, namespace=namespace, body=dep)
        except Exception as repl_err:
            logger.warning(
                "replace_namespaced_deployment failed (%s); falling back to sanitized OpenAPI patch",
                repl_err,
            )
            sanitized = api.api_client.sanitize_for_serialization(target_template)
            api.patch_namespaced_deployment(
                name=deployment_name,
                namespace=namespace,
                body={"spec": {"template": sanitized}},
            )

        return (
            f"Successfully rolled back {namespace}/{deployment_name} to stable revision {chosen_rev} "
            f"(ReplicaSet {chosen_rs.metadata.name})."
        )
    except Exception as e:
        return f"Error rolling back deployment: {str(e)}"

def remove_container_command(
    namespace: str, deployment_name: str, container_name: str | None = None
) -> str:
    """Remove faulty command/args override from a deployment container to restore default entrypoint."""
    api = _get_apps_api()
    try:
        dep = api.read_namespaced_deployment(name=deployment_name, namespace=namespace)
        containers = (
            dep.spec.template.spec.containers
            if (dep.spec and dep.spec.template and dep.spec.template.spec)
            else []
        )
        target_container = None
        for c in containers:
            if not container_name or c.name == container_name:
                c.command = None
                c.args = None
                target_container = c.name
                break

        if not target_container:
            return f"Container '{container_name or 'primary'}' not found in {namespace}/{deployment_name}"

        import datetime
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if not dep.spec.template.metadata:
            dep.spec.template.metadata = k8s_client.V1ObjectMeta()
        if not dep.spec.template.metadata.annotations:
            dep.spec.template.metadata.annotations = {}
        dep.spec.template.metadata.annotations["kubectl.kubernetes.io/restartedAt"] = now

        api.replace_namespaced_deployment(name=deployment_name, namespace=namespace, body=dep)
        return (
            f"Successfully removed command override for container '{target_container}' in "
            f"{namespace}/{deployment_name}; deployment rolled out with default entrypoint."
        )
    except Exception as e:
        return f"Error removing container command override: {str(e)}"


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
