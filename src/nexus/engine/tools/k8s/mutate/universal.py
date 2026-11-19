"""
Universal Kubernetes Dynamic Provider Tool Bridge
=================================================
Generic DynamicClient and CoreV1Api inspection, logs, and mutation bridge
supporting ANY Kubernetes resource (Deployment, StatefulSet, DaemonSet, Job,
PVC, Ingress, CRDs, etc.).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal

from kubernetes import client, config
from kubernetes.dynamic import DynamicClient
from pydantic import BaseModel, Field


def get_default_k8s_clients() -> tuple[DynamicClient, client.CoreV1Api]:
    """Initialize default Kubernetes dynamic and CoreV1 clients."""
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()
    api_client = client.ApiClient()
    return DynamicClient(api_client), client.CoreV1Api(api_client)


def _resolve_api_version(kind: str, current_api_version: str) -> str:
    """Auto-resolve api_version if generic default 'v1' is provided for apps resources."""
    k_lower = kind.lower()
    if current_api_version in ("v1", ""):
        if k_lower in (
            "deployment",
            "replicaset",
            "statefulset",
            "daemonset",
            "controllerrevision",
        ):
            return "apps/v1"
        if k_lower in ("horizontalpodautoscaler", "hpa"):
            return "autoscaling/v2"
        if k_lower in ("job", "cronjob"):
            return "batch/v1"
        if k_lower in ("ingress",):
            return "networking.k8s.io/v1"
    return current_api_version or "v1"


def _clean_k8s_manifest(data: Any) -> Any:
    """Strip verbose metadata (e.g. managedFields) to ensure status and ownerReferences fit."""
    if isinstance(data, dict):
        cleaned = {}
        for k, v in data.items():
            if k == "managedFields":
                continue
            cleaned[k] = _clean_k8s_manifest(v)
        return cleaned
    elif isinstance(data, list):
        return [_clean_k8s_manifest(x) for x in data]
    return data


class K8sInspectResourceInput(BaseModel):
    """Inspect or list any Kubernetes resource or custom resource (Pod, Deployment, ReplicaSet, etc.).
    Returns metadata with ownerReferences and status with containerStatuses/lastState.
    """

    api_version: str = Field(
        default="v1", description="e.g. 'v1', 'apps/v1', 'batch/v1'"
    )
    kind: str = Field(
        description="e.g. 'Pod', 'Deployment', 'StatefulSet', 'PersistentVolumeClaim'"
    )
    name: str | None = Field(default=None, description="Resource name or None to list")
    namespace: str = Field(default="default")
    label_selector: str | None = Field(
        default=None, description="Optional label selector"
    )


class K8sGetLogsInput(BaseModel):
    """Fetch logs from any pod or previous container instance."""

    pod_name: str
    namespace: str = Field(default="default")
    container: str | None = Field(default=None)
    tail_lines: int = Field(default=100, ge=10, le=500)
    previous: bool = Field(
        default=False, description="Fetch logs from crashed instance"
    )


class K8sMutateResourceInput(BaseModel):
    """Execute generic mutations on any Kubernetes resource."""

    api_version: str = Field(default="apps/v1", description="e.g. 'v1', 'apps/v1'")
    kind: str = Field(
        description="e.g. 'Deployment', 'StatefulSet', 'ConfigMap', 'Pod'"
    )
    name: str
    namespace: str = Field(default="default")
    mutation_type: Literal[
        "strategic_merge_patch",
        "scale_replicas",
        "rollout_restart",
        "delete_pod",
        "rollback",
    ]
    patch_body: dict[str, Any] = Field(
        default_factory=dict, description="Patch dictionary"
    )


def execute_k8s_inspect(
    args: K8sInspectResourceInput, dyn_client: DynamicClient | None = None
) -> str:
    """Inspect or list any Kubernetes resource, retaining ownerReferences and status with lastState."""
    try:
        if dyn_client is None:
            dyn_client, _ = get_default_k8s_clients()
        api_ver = _resolve_api_version(args.kind, args.api_version)
        resource_api = dyn_client.resources.get(api_version=api_ver, kind=args.kind)
        if args.name:
            obj = resource_api.get(name=args.name, namespace=args.namespace)
            data = obj.to_dict() if hasattr(obj, "to_dict") else obj
            cleaned = _clean_k8s_manifest(data)
            return json.dumps(cleaned, default=str)[:16000]
        items = resource_api.get(
            namespace=args.namespace, label_selector=args.label_selector
        )
        data = items.to_dict() if hasattr(items, "to_dict") else items
        cleaned = _clean_k8s_manifest(data)
        return json.dumps(cleaned, default=str)[:16000]
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def execute_k8s_get_logs(
    args: K8sGetLogsInput, core_v1: client.CoreV1Api | None = None
) -> str:
    """Fetch logs from any pod or previous container instance."""
    try:
        if core_v1 is None:
            _, core_v1 = get_default_k8s_clients()
        logs = core_v1.read_namespaced_pod_log(
            name=args.pod_name,
            namespace=args.namespace,
            container=args.container,
            tail_lines=args.tail_lines,
            previous=args.previous,
        )
        return str(logs)[-12000:]
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def execute_k8s_mutate(
    args: K8sMutateResourceInput, dyn_client: DynamicClient | None = None
) -> str:
    """Execute dynamic mutations on any Kubernetes resource (patch limits, scale, restart, rollback)."""
    try:
        if dyn_client is None:
            dyn_client, _ = get_default_k8s_clients()
        api_ver = _resolve_api_version(args.kind, args.api_version)
        resource_api = dyn_client.resources.get(api_version=api_ver, kind=args.kind)

        if args.mutation_type == "delete_pod" and args.kind.lower() in ("pod", "po"):
            res = resource_api.delete(name=args.name, namespace=args.namespace)
            data = res.to_dict() if hasattr(res, "to_dict") else res
            return json.dumps(_clean_k8s_manifest(data), default=str)[:6000]

        if args.mutation_type == "scale_replicas":
            replicas = int(args.patch_body["replicas"])
            scale_patch: dict[str, Any] = {"spec": {"replicas": replicas}}
            res = resource_api.patch(
                name=args.name,
                namespace=args.namespace,
                body=scale_patch,
                content_type="application/merge-patch+json",
            )
            data = res.to_dict() if hasattr(res, "to_dict") else res
            return json.dumps(_clean_k8s_manifest(data), default=str)[:6000]

        if args.mutation_type == "rollout_restart":
            now = datetime.now(timezone.utc).isoformat()
            restart_patch: dict[str, Any] = {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {"kubectl.kubernetes.io/restartedAt": now}
                        }
                    }
                }
            }
            res = resource_api.patch(
                name=args.name,
                namespace=args.namespace,
                body=restart_patch,
                content_type="application/strategic-merge-patch+json",
            )
            data = res.to_dict() if hasattr(res, "to_dict") else res
            return json.dumps(_clean_k8s_manifest(data), default=str)[:6000]

        if args.mutation_type == "rollback":
            from nexus.engine.tools.k8s.mutate.mutate_tools import rollback_deployment

            rev = args.patch_body.get("revision") or args.patch_body.get(
                "target_revision"
            )
            res_str = rollback_deployment(
                namespace=args.namespace,
                deployment_name=args.name,
                target_revision=int(rev) if rev is not None else None,
            )
            return json.dumps(
                {"status": res_str, "action": "rollback", "deployment": args.name}
            )

        # strategic_merge_patch (e.g. for patching limits/resources or config)
        res = resource_api.patch(
            name=args.name,
            namespace=args.namespace,
            body=args.patch_body,
            content_type="application/strategic-merge-patch+json",
        )
        data = res.to_dict() if hasattr(res, "to_dict") else res
        return json.dumps(_clean_k8s_manifest(data), default=str)[:6000]
    except Exception as exc:
        return json.dumps({"error": str(exc)})
