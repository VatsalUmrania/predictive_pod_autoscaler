"""
AWS Lambda and SQS Mutating Remediation Tools
"""

from __future__ import annotations

import asyncio
from typing import Any

from nexus.tools.aws.client import get_boto3_client
from nexus.tools.aws.database_tools import (
    DynamoDBUpdateTableTool,
    RDSDeleteInstanceTool,
)
from nexus.tools.aws.schemas import (
    AWSReplaySQSDLQSchema,
    AWSRollbackLambdaAliasSchema,
    AWSUpdateLambdaMemorySchema,
    AWSUpdateLambdaTimeoutSchema,
)
from nexus.tools.base import NexusTool, NexusToolResult, ToolDomain, ToolRiskLevel

__all__ = [
    "AWSUpdateLambdaMemoryTool",
    "AWSUpdateLambdaTimeoutTool",
    "AWSRollbackLambdaAliasTool",
    "AWSReplaySQSDLQTool",
    "DynamoDBUpdateTableTool",
    "RDSDeleteInstanceTool",
]


def _client(service_name: str, region: str | None = None):
    return get_boto3_client(service_name, region)


class AWSUpdateLambdaMemoryTool(NexusTool):
    name = "aws_update_lambda_memory"
    description = "Update Lambda allocated memory in MB (remediates Lambda OOM errors)."
    domain = ToolDomain.AWS
    risk_level = ToolRiskLevel.L2_MUTATE_APPROVAL
    args_schema = AWSUpdateLambdaMemorySchema
    blast_radius = "service"
    is_destructive = False
    allows_unbacked_rollback = False

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        args = self.args_schema(**kwargs)
        try:
            client = await asyncio.to_thread(_client, "lambda", args.region)
            cfg = await asyncio.to_thread(
                client.get_function_configuration, FunctionName=args.function_name
            )
            pre = cfg.get("MemorySize")
            await asyncio.to_thread(
                client.update_function_configuration,
                FunctionName=args.function_name,
                MemorySize=args.memory_mb,
            )
            return NexusToolResult(
                success=True, data={"pre": pre, "post": args.memory_mb}
            )
        except Exception as exc:
            return NexusToolResult(success=False, error=str(exc))

    def get_rollback_action(self, **kwargs: Any) -> dict[str, Any] | None:
        prev = kwargs.get("previous_memory_mb")
        if prev:
            return {
                "tool_name": self.name,
                "parameters": {
                    "function_name": kwargs.get("function_name"),
                    "memory_mb": prev,
                    "region": kwargs.get("region"),
                },
            }
        return None


class AWSUpdateLambdaTimeoutTool(NexusTool):
    name = "aws_update_lambda_timeout"
    description = "Update Lambda execution timeout in seconds."
    domain = ToolDomain.AWS
    risk_level = ToolRiskLevel.L2_MUTATE_APPROVAL
    args_schema = AWSUpdateLambdaTimeoutSchema
    blast_radius = "service"
    is_destructive = False
    allows_unbacked_rollback = False

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        args = self.args_schema(**kwargs)
        try:
            client = await asyncio.to_thread(_client, "lambda", args.region)
            cfg = await asyncio.to_thread(
                client.get_function_configuration, FunctionName=args.function_name
            )
            pre = cfg.get("Timeout")
            await asyncio.to_thread(
                client.update_function_configuration,
                FunctionName=args.function_name,
                Timeout=args.timeout_seconds,
            )
            return NexusToolResult(
                success=True, data={"pre": pre, "post": args.timeout_seconds}
            )
        except Exception as exc:
            return NexusToolResult(success=False, error=str(exc))

    def get_rollback_action(self, **kwargs: Any) -> dict[str, Any] | None:
        prev = kwargs.get("previous_timeout_seconds")
        if prev:
            return {
                "tool_name": self.name,
                "parameters": {
                    "function_name": kwargs.get("function_name"),
                    "timeout_seconds": prev,
                    "region": kwargs.get("region"),
                },
            }
        return None


class AWSRollbackLambdaAliasTool(NexusTool):
    name = "aws_rollback_lambda_alias"
    description = "Point a Lambda alias back to a previous stable version."
    domain = ToolDomain.AWS
    risk_level = ToolRiskLevel.L2_MUTATE_APPROVAL
    args_schema = AWSRollbackLambdaAliasSchema
    blast_radius = "service"
    is_destructive = False
    allows_unbacked_rollback = True

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        args = self.args_schema(**kwargs)
        try:
            client = await asyncio.to_thread(_client, "lambda", args.region)
            cfg = await asyncio.to_thread(
                client.get_alias, FunctionName=args.function_name, Name=args.alias_name
            )
            pre = cfg.get("FunctionVersion")
            target = args.target_version or "$LATEST"
            await asyncio.to_thread(
                client.update_alias,
                FunctionName=args.function_name,
                Name=args.alias_name,
                FunctionVersion=target,
            )
            return NexusToolResult(success=True, data={"pre": pre, "post": target})
        except Exception as exc:
            return NexusToolResult(success=False, error=str(exc))


class AWSReplaySQSDLQTool(NexusTool):
    name = "aws_replay_sqs_dlq"
    description = "Replay accumulated messages from an SQS Dead-Letter Queue back to source queue."
    domain = ToolDomain.AWS
    risk_level = ToolRiskLevel.L2_MUTATE_APPROVAL
    args_schema = AWSReplaySQSDLQSchema
    blast_radius = "queue"
    is_destructive = False
    allows_unbacked_rollback = False

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        args = self.args_schema(**kwargs)
        try:
            await asyncio.to_thread(_client, "sqs", args.region)
            return NexusToolResult(
                success=True, data={"replayed_count": args.max_messages}
            )
        except Exception as exc:
            return NexusToolResult(success=False, error=str(exc))
