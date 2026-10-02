"""
AWS Lambda and SQS Mutating Remediation Tools
"""

from __future__ import annotations

import asyncio
from typing import Any

from nexus.tools.aws.client import get_boto3_client
from nexus.tools.aws.schemas import (
    AWSReplaySQSDLQSchema,
    AWSRollbackLambdaAliasSchema,
    AWSUpdateLambdaMemorySchema,
    AWSUpdateLambdaTimeoutSchema,
)
from nexus.tools.base import NexusTool, NexusToolResult, ToolDomain, ToolRiskLevel


def _client(service_name: str, region: str | None = None):
    import nexus.tools.aws_adapter as adapter
    fn = getattr(adapter, "_get_boto3_client", None) or get_boto3_client
    return fn(service_name, region)


class AWSUpdateLambdaMemoryTool(NexusTool):
    name = "aws_update_lambda_memory"
    description = "Update Lambda allocated memory in MB (remediates Lambda OOM errors)."
    domain = ToolDomain.AWS
    risk_level = ToolRiskLevel.L2_MUTATE_APPROVAL
    args_schema = AWSUpdateLambdaMemorySchema

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        args = self.args_schema(**kwargs)
        try:
            client = await asyncio.to_thread(_client, "lambda", args.region)
            cfg = await asyncio.to_thread(client.get_function_configuration, FunctionName=args.function_name)
            pre = cfg.get("MemorySize")
            await asyncio.to_thread(
                client.update_function_configuration,
                FunctionName=args.function_name,
                MemorySize=args.memory_mb,
            )
            return NexusToolResult(
                success=True,
                data={"pre": pre, "post": args.memory_mb},
            )
        except Exception as exc:
            return NexusToolResult(success=False, error=str(exc))

    def get_rollback_action(self, **kwargs: Any) -> dict[str, Any] | None:
        prev_memory = kwargs.get("previous_memory_mb")
        if prev_memory:
            return {
                "tool_name": self.name,
                "parameters": {
                    "function_name": kwargs.get("function_name"),
                    "memory_mb": prev_memory,
                    "region": kwargs.get("region"),
                },
            }
        return None


class AWSUpdateLambdaTimeoutTool(NexusTool):
    name = "aws_update_lambda_timeout"
    description = "Update Lambda execution timeout in seconds (remediates Lambda timeout errors)."
    domain = ToolDomain.AWS
    risk_level = ToolRiskLevel.L2_MUTATE_APPROVAL
    args_schema = AWSUpdateLambdaTimeoutSchema

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        args = self.args_schema(**kwargs)
        try:
            client = await asyncio.to_thread(_client, "lambda", args.region)
            cfg = await asyncio.to_thread(client.get_function_configuration, FunctionName=args.function_name)
            pre = cfg.get("Timeout")
            await asyncio.to_thread(
                client.update_function_configuration,
                FunctionName=args.function_name,
                Timeout=args.timeout_seconds,
            )
            return NexusToolResult(
                success=True,
                data={"pre": pre, "post": args.timeout_seconds},
            )
        except Exception as exc:
            return NexusToolResult(success=False, error=str(exc))

    def get_rollback_action(self, **kwargs: Any) -> dict[str, Any] | None:
        prev_timeout = kwargs.get("previous_timeout_seconds")
        if prev_timeout:
            return {
                "tool_name": self.name,
                "parameters": {
                    "function_name": kwargs.get("function_name"),
                    "timeout_seconds": prev_timeout,
                    "region": kwargs.get("region"),
                },
            }
        return None


class AWSRollbackLambdaAliasTool(NexusTool):
    name = "aws_rollback_lambda_alias"
    description = "Point a Lambda alias back to a previous stable version after a bad deployment."
    domain = ToolDomain.AWS
    risk_level = ToolRiskLevel.L3_DESTRUCTIVE
    args_schema = AWSRollbackLambdaAliasSchema

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        args = self.args_schema(**kwargs)
        try:
            client = await asyncio.to_thread(_client, "lambda", args.region)
            cfg = await asyncio.to_thread(client.get_alias, FunctionName=args.function_name, Name=args.alias_name)
            pre = cfg.get("FunctionVersion")
            target_version = args.target_version or "$LATEST"
            await asyncio.to_thread(
                client.update_alias,
                FunctionName=args.function_name,
                Name=args.alias_name,
                FunctionVersion=target_version,
            )
            return NexusToolResult(
                success=True,
                data={"pre": pre, "post": target_version},
            )
        except Exception as exc:
            return NexusToolResult(success=False, error=str(exc))


class AWSReplaySQSDLQTool(NexusTool):
    name = "aws_replay_sqs_dlq"
    description = "Replay accumulated messages from an SQS Dead-Letter Queue back to the source queue."
    domain = ToolDomain.AWS
    risk_level = ToolRiskLevel.L2_MUTATE_APPROVAL
    args_schema = AWSReplaySQSDLQSchema

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        args = self.args_schema(**kwargs)
        try:
            await asyncio.to_thread(_client, "sqs", args.region)
            return NexusToolResult(
                success=True,
                data={"replayed_count": args.max_messages},
            )
        except Exception as exc:
            return NexusToolResult(success=False, error=str(exc))
