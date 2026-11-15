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


class K8sInspectResourceInput(BaseModel):
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
    pod_name: str
    namespace: str = Field(default="default")
    container: str | None = Field(default=None)
    tail_lines: int = Field(default=100, ge=10, le=500)
    previous: bool = Field(
        default=False, description="Fetch logs from crashed instance"
    )


class K8sMutateResourceInput(BaseModel):
    api_version: str = Field(default="apps/v1", description="e.g. 'v1', 'apps/v1'")
    kind: str = Field(
        description="e.g. 'Deployment', 'StatefulSet', 'ConfigMap', 'Pod'"
    )
    name: str
    namespace: str = Field(default="default")
    mutation_type: Literal[
        "strategic_merge_patch", "scale_replicas", "rollout_restart", "delete_pod"
    ]
    patch_body: dict[str, Any] = Field(
        default_factory=dict, description="Patch dictionary"
    )


def execute_k8s_inspect(
    args: K8sInspectResourceInput, dyn_client: DynamicClient | None = None
) -> str:
    """Inspect or list any Kubernetes resource or custom resource."""
    try:
        if dyn_client is None:
            dyn_client, _ = get_default_k8s_clients()
        resource_api = dyn_client.resources.get(
            api_version=args.api_version, kind=args.kind
        )
        if args.name:
            obj = resource_api.get(name=args.name, namespace=args.namespace)
            data = obj.to_dict() if hasattr(obj, "to_dict") else obj
            return json.dumps(data, default=str)[:12000]
        items = resource_api.get(
            namespace=args.namespace, label_selector=args.label_selector
        )
        data = items.to_dict() if hasattr(items, "to_dict") else items
        return json.dumps(data, default=str)[:12000]
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
    """Execute dynamic mutations on any Kubernetes resource."""
    try:
        if dyn_client is None:
            dyn_client, _ = get_default_k8s_clients()
        resource_api = dyn_client.resources.get(
            api_version=args.api_version, kind=args.kind
        )

        if args.mutation_type == "delete_pod" and args.kind == "Pod":
            res = resource_api.delete(name=args.name, namespace=args.namespace)
            data = res.to_dict() if hasattr(res, "to_dict") else res
            return json.dumps(data, default=str)[:6000]

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
            return json.dumps(data, default=str)[:6000]

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
            return json.dumps(data, default=str)[:6000]

        res = resource_api.patch(
            name=args.name,
            namespace=args.namespace,
            body=args.patch_body,
            content_type="application/strategic-merge-patch+json",
        )
        data = res.to_dict() if hasattr(res, "to_dict") else res
        return json.dumps(data, default=str)[:6000]
    except Exception as exc:
        return json.dumps({"error": str(exc)})
