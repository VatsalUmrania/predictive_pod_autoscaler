"""
AWS CloudWatch Metric & Log Observation Tools
"""

from __future__ import annotations

import asyncio
from typing import Any

from nexus.tools.aws.client import get_boto3_client
from nexus.tools.aws.schemas import (
    AWSGetLogEventsSchema,
    AWSGetMetricDataSchema,
    DynamoDBDescribeTableSchema,
)
from nexus.tools.base import NexusTool, NexusToolResult, ToolDomain, ToolRiskLevel


def _client(service_name: str, region: str | None = None):
    return get_boto3_client(service_name, region)


class AWSGetMetricDataTool(NexusTool):
    name = "aws_get_metric_data"
    description = (
        "Query CloudWatch metrics for AWS resources (Lambda, DynamoDB, SQS, ECS)."
    )
    domain = ToolDomain.AWS
    risk_level = ToolRiskLevel.L0_OBSERVE
    args_schema = AWSGetMetricDataSchema
    blast_radius = "none"
    is_destructive = False
    allows_unbacked_rollback = False

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        args = self.args_schema(**kwargs)
        try:
            from datetime import datetime, timedelta, timezone

            cw = await asyncio.to_thread(_client, "cloudwatch", args.region)
            end_time = datetime.now(timezone.utc)
            start_time = end_time - timedelta(minutes=15)

            resp = await asyncio.to_thread(
                cw.get_metric_statistics,
                Namespace=args.namespace,
                MetricName=args.metric_name,
                Dimensions=[
                    {"Name": args.dimension_name, "Value": args.dimension_value}
                ],
                StartTime=start_time,
                EndTime=end_time,
                Period=args.period_seconds,
                Statistics=(
                    [args.stat]
                    if args.stat in ("Sum", "Average", "Maximum", "Minimum")
                    else []
                ),
                ExtendedStatistics=[args.stat] if args.stat.startswith("p") else [],
            )
            raw_dp = resp.get("Datapoints", [])
            datapoints = (
                [d for d in raw_dp if isinstance(d, dict)]
                if isinstance(raw_dp, (list, tuple))
                else []
            )
            return NexusToolResult(
                success=True, data={"datapoints": datapoints, "count": len(datapoints)}
            )
        except Exception as exc:
            return NexusToolResult(success=False, error=str(exc))


class AWSGetLogEventsTool(NexusTool):
    name = "aws_get_log_events"
    description = "Search CloudWatch log streams for error messages and stack traces."
    domain = ToolDomain.AWS
    risk_level = ToolRiskLevel.L0_OBSERVE
    args_schema = AWSGetLogEventsSchema
    blast_radius = "none"
    is_destructive = False
    allows_unbacked_rollback = False

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        args = self.args_schema(**kwargs)
        try:
            logs = await asyncio.to_thread(_client, "logs", args.region)
            kwargs_call: dict[str, Any] = {
                "logGroupName": args.log_group_name,
                "limit": args.limit,
            }
            if args.filter_pattern:
                kwargs_call["filterPattern"] = args.filter_pattern

            resp = await asyncio.to_thread(logs.filter_log_events, **kwargs_call)
            raw_evts = resp.get("events", [])
            events = (
                [e.get("message", "") for e in raw_evts if isinstance(e, dict)]
                if isinstance(raw_evts, (list, tuple))
                else []
            )
            return NexusToolResult(
                success=True, data={"events": events, "count": len(events)}
            )
        except Exception as exc:
            return NexusToolResult(success=False, error=str(exc))


class DynamoDBDescribeTableTool(NexusTool):
    name = "aws_dynamodb_describe_table"
    description = (
        "Describe DynamoDB table configuration, provisioned capacity, and status."
    )
    domain = ToolDomain.AWS
    risk_level = ToolRiskLevel.L0_OBSERVE
    args_schema = DynamoDBDescribeTableSchema
    blast_radius = "none"
    is_destructive = False
    allows_unbacked_rollback = False

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        args = self.args_schema(**kwargs)
        try:
            client = await asyncio.to_thread(_client, "dynamodb", args.region)
            resp = await asyncio.to_thread(
                client.describe_table, TableName=args.table_name
            )
            table = resp.get("Table", {})
            return NexusToolResult(
                success=True,
                data={
                    "table_name": table.get("TableName"),
                    "status": table.get("TableStatus"),
                    "item_count": table.get("ItemCount"),
                    "table_size_bytes": table.get("TableSizeBytes"),
                    "provisioned_throughput": table.get("ProvisionedThroughput"),
                    "billing_mode_summary": table.get("BillingModeSummary"),
                },
            )
        except Exception as exc:
            return NexusToolResult(success=False, error=str(exc))
