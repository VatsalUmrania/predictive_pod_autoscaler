"""
Kubernetes Deployment Rollback and Override Removal Tools
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from kubernetes import client as k8s_client

from nexus.agents.k8s_tools.client import _get_apps_api

logger = logging.getLogger(__name__)


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
            curr_containers = (
                dep.spec.template.spec.containers
                if (dep.spec and dep.spec.template and dep.spec.template.spec)
                else []
            )
            curr_c0 = curr_containers[0] if curr_containers else None
            curr_command = getattr(curr_c0, "command", None)

            for rs in sorted_rs[1:]:
                rs_containers = (
                    rs.spec.template.spec.containers
                    if (rs.spec and rs.spec.template and rs.spec.template.spec)
                    else []
                )
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

        if target_template.metadata and target_template.metadata.labels:
            target_template.metadata.labels.pop("pod-template-hash", None)

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if not target_template.metadata:
            target_template.metadata = k8s_client.V1ObjectMeta()
        if not target_template.metadata.annotations:
            target_template.metadata.annotations = {}
        target_template.metadata.annotations["kubectl.kubernetes.io/restartedAt"] = now

        dep.spec.template = target_template

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
