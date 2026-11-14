"""
AWS snapshot creation for rollbacks.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from nexus.graph.platform.base import RollbackSnapshot, TargetResource


def _get_client(service: str, region: str) -> Any:
    import nexus.graph.platform.aws as aws_plat

    return aws_plat._get_boto3_client(service, region)


async def capture_aws_snapshot(
    target: TargetResource, tool_name: str, parameters: dict[str, Any]
) -> RollbackSnapshot | None:
    """Capture AWS pre-mutation state for deterministic rollback."""
    snap_id = str(uuid.uuid4())[:8]

    if tool_name == "aws_update_lambda_memory":
        prior_memory = 128
        try:
            client = await asyncio.to_thread(_get_client, "lambda", target.namespace)
            cfg = await asyncio.to_thread(
                client.get_function_configuration, FunctionName=target.name
            )
            prior_memory = cfg.get("MemorySize", 128)
        except Exception:
            pass

        return RollbackSnapshot(
            snapshot_id=f"aws-mem-{snap_id}",
            target=target,
            action_name=tool_name,
            rollback_tool="aws_update_lambda_memory",
            rollback_parameters={
                "function_name": target.name,
                "memory_mb": prior_memory,
                "region": target.namespace,
            },
            pre_mutation_state={"memory_mb": prior_memory},
            description=f"Rollback Lambda memory to {prior_memory}MB",
        )

    if tool_name == "aws_update_lambda_timeout":
        prior_timeout = 30
        try:
            client = await asyncio.to_thread(_get_client, "lambda", target.namespace)
            cfg = await asyncio.to_thread(
                client.get_function_configuration, FunctionName=target.name
            )
            prior_timeout = cfg.get("Timeout", 30)
        except Exception:
            pass

        return RollbackSnapshot(
            snapshot_id=f"aws-timeout-{snap_id}",
            target=target,
            action_name=tool_name,
            rollback_tool="aws_update_lambda_timeout",
            rollback_parameters={
                "function_name": target.name,
                "timeout_seconds": prior_timeout,
                "region": target.namespace,
            },
            pre_mutation_state={"timeout_seconds": prior_timeout},
            description=f"Rollback Lambda timeout to {prior_timeout}s",
        )

    if tool_name == "aws_rollback_lambda_alias":
        return RollbackSnapshot(
            snapshot_id=f"aws-alias-{snap_id}",
            target=target,
            action_name=tool_name,
            rollback_tool="aws_rollback_lambda_alias",
            rollback_parameters={
                "function_name": target.name,
                "alias_name": parameters.get("alias_name", "live"),
                "target_version": "$LATEST",
                "region": target.namespace,
            },
            description="Rollback Lambda alias version",
        )

    return None
