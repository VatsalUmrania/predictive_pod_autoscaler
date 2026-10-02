"""
Kubernetes Read Tools for LLM Orchestrator
"""

from __future__ import annotations

import logging

import yaml

from nexus.agents.k8s_tools.client import _get_api, _get_apps_api

logger = logging.getLogger(__name__)


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


def get_events(namespace: str, resource_name: str | None = None) -> str:
    """Get recent events in a namespace, optionally filtered by resource."""
    api = _get_api()
    try:
        events = api.list_namespaced_event(namespace=namespace)
        relevant_events = []
        for event in events.items:
            if not resource_name or (
                event.involved_object and event.involved_object.name == resource_name
            ):
                relevant_events.append(f"{event.type}: {event.message} ({event.reason})")
        return "\n".join(relevant_events) if relevant_events else "No recent events found."
    except Exception as e:
        return f"Error fetching events: {str(e)}"


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

            if obj.spec and obj.spec.template and obj.spec.template.spec and obj.spec.template.spec.containers:
                for c in obj.spec.template.spec.containers:
                    lines.append(f"Container Spec [{c.name}]: image={c.image}, command={c.command}, args={c.args}")

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

        data = obj.to_dict()
        for key in ["status", "managed_fields", "creation_timestamp", "resource_version", "uid"]:
            data.pop(key, None)
        if "metadata" in data:
            for key in ["creation_timestamp", "resource_version", "uid", "generation"]:
                data["metadata"].pop(key, None)
        return str(yaml.dump(data, default_flow_style=False))

    except Exception as e:
        return f"Error fetching YAML for {resource_kind} {resource_name}: {str(e)}"
