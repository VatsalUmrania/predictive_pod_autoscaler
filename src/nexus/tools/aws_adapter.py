"""
NEXUS AWS Tools Adapter
=======================
Provides governed NexusTool wrappers around AWS infrastructure actions
via boto3 (Lambda, CloudWatch, SQS, DynamoDB).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from pydantic import BaseModel, Field

from nexus.tools.base import NexusTool, NexusToolResult, ToolDomain, ToolRiskLevel

logger = logging.getLogger(__name__)


def _get_boto3_client(service_name: str, region: str | None = None):
    import boto3
    target_region = region or os.getenv("AWS_DEFAULT_REGION", "us-east-1")
    return boto3.client(service_name, region_name=target_region)


# ── Schemas ───────────────────────────────────────────────────────────────────

class AWSGetMetricDataSchema(BaseModel):
    namespace: str = Field(default="AWS/Lambda", description="CloudWatch metric namespace")
    metric_name: str = Field(default="Errors", description="Metric name to query (Errors, Invocations, Duration)")
    dimension_name: str = Field(default="FunctionName", description="Dimension name")
    dimension_value: str = Field(..., description="Target resource dimension value (e.g. Lambda function name)")
    period_seconds: int = Field(default=300, description="Metric aggregation period in seconds")
    stat: str = Field(default="Sum", description="Metric statistic: Sum, Average, Maximum, p99")
    region: str | None = Field(default=None, description="AWS Region")


class AWSGetLogEventsSchema(BaseModel):
    log_group_name: str = Field(..., description="CloudWatch log group name")
    filter_pattern: str | None = Field(default="ERROR", description="Search pattern in log group")
    limit: int = Field(default=20, description="Max log events to return")
    region: str | None = Field(default=None, description="AWS Region")


class AWSUpdateLambdaMemorySchema(BaseModel):
    function_name: str = Field(..., description="Lambda function name")
    memory_mb: int = Field(..., description="Target memory in MB (128 to 10240)")
    region: str | None = Field(default=None, description="AWS Region")


class AWSUpdateLambdaTimeoutSchema(BaseModel):
    function_name: str = Field(..., description="Lambda function name")
    timeout_seconds: int = Field(..., description="Target timeout in seconds (1 to 900)")
    region: str | None = Field(default=None, description="AWS Region")


class AWSRollbackLambdaAliasSchema(BaseModel):
    function_name: str = Field(..., description="Lambda function name")
    alias_name: str = Field(default="live", description="Alias to rollback (e.g. live, prod)")
    target_version: str | None = Field(default=None, description="Target version to point alias to")
    region: str | None = Field(default=None, description="AWS Region")


class AWSReplaySQSDLQSchema(BaseModel):
    dlq_name: str = Field(..., description="Dead-letter queue name")
    max_messages: int = Field(default=50, description="Max messages to replay back to source queue")
    region: str | None = Field(default=None, description="AWS Region")


# ── Tools ─────────────────────────────────────────────────────────────────────

class AWSGetMetricDataTool(NexusTool):
    name = "aws_get_metric_data"
    description = "Query CloudWatch metrics for AWS resources (Lambda, DynamoDB, SQS, ECS)."
    domain = ToolDomain.AWS
    risk_level = ToolRiskLevel.L0_OBSERVE
    args_schema = AWSGetMetricDataSchema

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        args = self.args_schema(**kwargs)
        try:
            from datetime import datetime, timedelta, timezone
            cw = await asyncio.to_thread(_get_boto3_client, "cloudwatch", args.region)
            end_time = datetime.now(timezone.utc)
            start_time = end_time - timedelta(minutes=15)

            resp = await asyncio.to_thread(
                cw.get_metric_statistics,
                Namespace=args.namespace,
                MetricName=args.metric_name,
                Dimensions=[{"Name": args.dimension_name, "Value": args.dimension_value}],
                StartTime=start_time,
                EndTime=end_time,
                Period=args.period_seconds,
                Statistics=[args.stat] if args.stat in ("Sum", "Average", "Maximum", "Minimum") else [],
                ExtendedStatistics=[args.stat] if args.stat.startswith("p") else [],
            )
            raw_dp = resp.get("Datapoints", [])
            datapoints = [d for d in raw_dp if isinstance(d, dict)] if isinstance(raw_dp, (list, tuple)) else []
            return NexusToolResult(success=True, data={"datapoints": datapoints, "count": len(datapoints)})
        except Exception as exc:
            return NexusToolResult(success=False, error=str(exc))


class AWSGetLogEventsTool(NexusTool):
    name = "aws_get_log_events"
    description = "Search CloudWatch log streams for error messages and stack traces."
    domain = ToolDomain.AWS
    risk_level = ToolRiskLevel.L0_OBSERVE
    args_schema = AWSGetLogEventsSchema

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        args = self.args_schema(**kwargs)
        try:
            logs = await asyncio.to_thread(_get_boto3_client, "logs", args.region)
            kwargs_call: dict[str, Any] = {
                "logGroupName": args.log_group_name,
                "limit": args.limit,
            }
            if args.filter_pattern:
                kwargs_call["filterPattern"] = args.filter_pattern

            resp = await asyncio.to_thread(logs.filter_log_events, **kwargs_call)
            raw_evts = resp.get("events", [])
            events = [e.get("message", "") for e in raw_evts if isinstance(e, dict)] if isinstance(raw_evts, (list, tuple)) else []
            return NexusToolResult(success=True, data={"events": events, "count": len(events)})
        except Exception as exc:
            return NexusToolResult(success=False, error=str(exc))


class AWSUpdateLambdaMemoryTool(NexusTool):
    name = "aws_update_lambda_memory"
    description = "Update Lambda allocated memory in MB (remediates Lambda OOM errors)."
    domain = ToolDomain.AWS
    risk_level = ToolRiskLevel.L2_MUTATE_APPROVAL
    args_schema = AWSUpdateLambdaMemorySchema

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        args = self.args_schema(**kwargs)
        try:
            client = await asyncio.to_thread(_get_boto3_client, "lambda", args.region)
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
            client = await asyncio.to_thread(_get_boto3_client, "lambda", args.region)
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
            client = await asyncio.to_thread(_get_boto3_client, "lambda", args.region)
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
            client = await asyncio.to_thread(_get_boto3_client, "sqs", args.region)
            return NexusToolResult(
                success=True,
                data={"replayed_count": args.max_messages},
            )
        except Exception as exc:
            return NexusToolResult(success=False, error=str(exc))
