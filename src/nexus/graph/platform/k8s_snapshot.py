"""
Kubernetes snapshot creation for rollbacks.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any

from nexus.graph.platform.base import RollbackSnapshot, TargetResource


async def capture_k8s_snapshot(
    target: TargetResource, tool_name: str, parameters: dict[str, Any]
) -> RollbackSnapshot | None:
    """Capture rollback snapshot before mutation."""
    from nexus.agents import k8s_tools

    snap_id = str(uuid.uuid4())[:8]
    if tool_name == "k8s_scale_resource":
        desc = await asyncio.to_thread(
            k8s_tools.describe_resource,
            namespace=target.namespace,
            resource_kind=parameters.get("resource_kind", "deployment"),
            resource_name=parameters.get("resource_name", target.name),
        )
        old_replicas = 1
        m = re.search(r"Replicas:\s*(\d+)", desc)
        if m:
            old_replicas = int(m.group(1))

        return RollbackSnapshot(
            snapshot_id=f"k8s-scale-{snap_id}",
            target=target,
            action_name=tool_name,
            rollback_tool="k8s_scale_resource",
            rollback_parameters={
                "namespace": target.namespace,
                "resource_kind": parameters.get("resource_kind", "deployment"),
                "resource_name": parameters.get("resource_name", target.name),
                "replicas": old_replicas,
            },
            pre_mutation_state={"replicas": old_replicas},
            description=f"Rollback scale to {old_replicas} replicas",
        )

    if tool_name in ("k8s_restart_deployment", "k8s_rollback_deployment"):
        return RollbackSnapshot(
            snapshot_id=f"k8s-undo-{snap_id}",
            target=target,
            action_name=tool_name,
            rollback_tool="k8s_rollback_deployment",
            rollback_parameters={
                "namespace": target.namespace,
                "deployment_name": parameters.get("deployment_name", target.name),
            },
            description=f"Rollback deployment revision for {target.name}",
        )

    return None
