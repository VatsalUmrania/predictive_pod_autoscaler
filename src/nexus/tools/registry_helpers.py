"""
Helpers and backward-compatibility methods for NEXUS ToolRegistry.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from nexus.tools.aws.universal import (
    AWSInspectInput,
    AWSMutateInput,
    AWSQueryTelemetryInput,
    execute_aws_inspect,
    execute_aws_mutate,
    execute_aws_query_telemetry,
)
from nexus.tools.base import NexusToolResult, ToolRiskLevel
from nexus.tools.k8s.universal import (
    K8sGetLogsInput,
    K8sInspectResourceInput,
    K8sMutateResourceInput,
    execute_k8s_get_logs,
    execute_k8s_inspect,
    execute_k8s_mutate,
)

logger = logging.getLogger(__name__)


def register_universal_tools(registry: Any) -> None:
    registry.register_function("aws_inspect_resource", execute_aws_inspect, AWSInspectInput, is_mutating=False, provider="aws")
    registry.register_function("aws_query_telemetry", execute_aws_query_telemetry, AWSQueryTelemetryInput, is_mutating=False, provider="aws")
    registry.register_function("aws_mutate_resource", execute_aws_mutate, AWSMutateInput, is_mutating=True, provider="aws")
    registry.register_function("k8s_inspect_resource", execute_k8s_inspect, K8sInspectResourceInput, is_mutating=False, provider="kubernetes")
    registry.register_function("k8s_get_logs", execute_k8s_get_logs, K8sGetLogsInput, is_mutating=False, provider="kubernetes")
    registry.register_function("k8s_mutate_resource", execute_k8s_mutate, K8sMutateResourceInput, is_mutating=True, provider="kubernetes")


def build_tool_schemas(universal_callables: dict, tools: dict, is_mutating: bool) -> list[dict[str, Any]]:
    schemas = [
        {"name": n, "provider": p, "is_mutating": is_mutating, "parameters": s.model_json_schema()}
        for n, (_, s, m, p) in universal_callables.items()
        if m == is_mutating
    ]
    for t in tools.values():
        is_tool_mutating = getattr(t, "risk_level", None) != ToolRiskLevel.L0_OBSERVE
        if is_tool_mutating == is_mutating:
            sch = getattr(t, "args_schema", None)
            params = sch.model_json_schema() if sch and hasattr(sch, "model_json_schema") else {}
            schemas.append({
                "name": t.name,
                "provider": getattr(t.domain, "value", "unknown"),
                "is_mutating": is_mutating,
                "parameters": params,
            })
    return schemas


def register_legacy_tools(registry: Any) -> None:
    try:
        from nexus.tools.k8s_adapter import (
            K8sDescribeResourceTool,
            K8sGetMetricsTool,
            K8sGetPodLogsTool,
            K8sPatchResourceLimitsTool,
            K8sRemoveContainerCommandTool,
            K8sRestartDeploymentTool,
            K8sRollbackDeploymentTool,
            K8sScaleResourceTool,
        )

        for cls in (
            K8sDescribeResourceTool,
            K8sGetMetricsTool,
            K8sGetPodLogsTool,
            K8sPatchResourceLimitsTool,
            K8sRemoveContainerCommandTool,
            K8sRestartDeploymentTool,
            K8sRollbackDeploymentTool,
            K8sScaleResourceTool,
        ):
            registry.register(cls())
    except Exception as exc:
        logger.debug("[ToolRegistry] Could not load k8s adapter tools: %s", exc)

    try:
        from nexus.tools.aws_adapter import (
            AWSGetLogEventsTool,
            AWSGetMetricDataTool,
            AWSReplaySQSDLQTool,
            AWSRollbackLambdaAliasTool,
            AWSUpdateLambdaMemoryTool,
            AWSUpdateLambdaTimeoutTool,
            DynamoDBDescribeTableTool,
            DynamoDBUpdateTableTool,
            RDSDeleteInstanceTool,
        )

        for aws_cls in (
            AWSGetLogEventsTool,
            AWSGetMetricDataTool,
            AWSReplaySQSDLQTool,
            AWSRollbackLambdaAliasTool,
            AWSUpdateLambdaMemoryTool,
            AWSUpdateLambdaTimeoutTool,
            DynamoDBDescribeTableTool,
            DynamoDBUpdateTableTool,
            RDSDeleteInstanceTool,
        ):
            registry.register(aws_cls())
    except Exception as exc:
        logger.debug("[ToolRegistry] Could not load aws adapter tools: %s", exc)


async def execute_tool_with_db(
    registry: Any,
    name: str,
    parameters: dict[str, Any],
    run_id: str | None = None,
    db_client: Any = None,
) -> NexusToolResult:
    tool = registry.get(name)
    if not tool:
        return NexusToolResult(
            success=False, error=f"Tool '{name}' not found in registry"
        )
    call_id, start_time = None, time.monotonic()
    category = getattr(getattr(tool, "domain", None), "value", "k8s")
    if db_client and run_id:
        try:
            if hasattr(db_client, "log_tool_call"):
                try:
                    call_id = await db_client.log_tool_call(
                        run_id=run_id,
                        tool_name=name,
                        tool_category=category,
                        input_parameters=parameters,
                    )
                except TypeError:
                    call_id = await db_client.log_tool_call(
                        run_id=run_id, tool_name=name, input_parameters=parameters
                    )
        except Exception as log_exc:
            logger.warning("[ToolRegistry] Failed to log invocation: %s", log_exc)
    try:
        raw_res = await tool.execute(**parameters)
        result: NexusToolResult = (
            raw_res
            if isinstance(raw_res, NexusToolResult)
            else NexusToolResult(success=True, data=raw_res)
        )
    except Exception as exc:
        logger.error("[ToolRegistry] Error executing %s: %s", name, exc, exc_info=True)
        result = NexusToolResult(success=False, error=str(exc))
    duration_ms = int((time.monotonic() - start_time) * 1000)
    if db_client and call_id:
        try:
            status = "success" if result.success else "failed"
            if hasattr(db_client, "update_tool_call"):
                await db_client.update_tool_call(
                    call_id=call_id,
                    status=status,
                    output_result=result.data,
                    error_details=result.error,
                    duration_ms=duration_ms,
                )
            elif hasattr(db_client, "update_tool_call_result"):
                await db_client.update_tool_call_result(
                    call_id=call_id,
                    status=status,
                    output_data=result.data,
                    error_message=result.error,
                    duration_ms=duration_ms,
                )
        except Exception as log_exc:
            logger.warning("[ToolRegistry] Failed to update outcome: %s", log_exc)
    return result
