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
    DynamoDBUpdateTableSchema,
    RDSDeleteInstanceSchema,
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
    description = (
        "Update Lambda execution timeout in seconds (remediates Lambda timeout errors)."
    )
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
    description = (
        "Point a Lambda alias back to a previous stable version after a bad deployment."
    )
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
    blast_radius = "queue"
    is_destructive = False
    allows_unbacked_rollback = False

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


class DynamoDBUpdateTableTool(NexusTool):
    name = "aws_dynamodb_update_table"
    description = "Update DynamoDB table provisioned throughput or billing mode (remediates write/read throttling)."
    domain = ToolDomain.AWS
    risk_level = ToolRiskLevel.L2_MUTATE_APPROVAL
    args_schema = DynamoDBUpdateTableSchema
    blast_radius = "service_wide"
    is_destructive = False
    allows_unbacked_rollback = False

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        args = self.args_schema(**kwargs)
        try:
            client = await asyncio.to_thread(_client, "dynamodb", args.region)
            desc = await asyncio.to_thread(
                client.describe_table, TableName=args.table_name
            )
            table_info = desc.get("Table", {})
            curr_throughput = table_info.get("ProvisionedThroughput", {})
            curr_rcu = curr_throughput.get("ReadCapacityUnits")
            curr_wcu = curr_throughput.get("WriteCapacityUnits")
            curr_billing = table_info.get("BillingModeSummary", {}).get(
                "BillingMode", "PROVISIONED"
            )

            pre = {
                "billing_mode": curr_billing,
                "read_capacity_units": curr_rcu,
                "write_capacity_units": curr_wcu,
            }

            update_kwargs: dict[str, Any] = {"TableName": args.table_name}
            if args.billing_mode:
                update_kwargs["BillingMode"] = args.billing_mode

            target_rcu = (
                args.read_capacity_units
                if args.read_capacity_units is not None
                else curr_rcu
            )
            target_wcu = (
                args.write_capacity_units
                if args.write_capacity_units is not None
                else curr_wcu
            )

            if (
                args.read_capacity_units is not None
                or args.write_capacity_units is not None
            ) and (args.billing_mode != "PAY_PER_REQUEST"):
                throughput: dict[str, int] = {}
                if target_rcu is not None:
                    throughput["ReadCapacityUnits"] = target_rcu
                if target_wcu is not None:
                    throughput["WriteCapacityUnits"] = target_wcu
                if throughput:
                    update_kwargs["ProvisionedThroughput"] = throughput

            await asyncio.to_thread(client.update_table, **update_kwargs)
            return NexusToolResult(
                success=True,
                data={
                    "table_name": args.table_name,
                    "pre": pre,
                    "post": {
                        "billing_mode": args.billing_mode or curr_billing,
                        "read_capacity_units": target_rcu,
                        "write_capacity_units": target_wcu,
                    },
                },
            )
        except Exception as exc:
            return NexusToolResult(success=False, error=str(exc))

    def get_rollback_action(self, **kwargs: Any) -> dict[str, Any] | None:
        prev_wcu = kwargs.get("previous_write_capacity_units")
        prev_rcu = kwargs.get("previous_read_capacity_units")
        prev_billing = kwargs.get("previous_billing_mode")
        if prev_wcu is not None or prev_rcu is not None or prev_billing is not None:
            params: dict[str, Any] = {
                "table_name": kwargs.get("table_name"),
                "region": kwargs.get("region"),
            }
            if prev_wcu is not None:
                params["write_capacity_units"] = prev_wcu
            if prev_rcu is not None:
                params["read_capacity_units"] = prev_rcu
            if prev_billing is not None:
                params["billing_mode"] = prev_billing
            return {
                "tool_name": self.name,
                "parameters": params,
            }
        return None


class RDSDeleteInstanceTool(NexusTool):
    name = "aws_rds_delete_db_instance"
    description = "Delete an RDS DB instance (destructive stateful datastore action)."
    domain = ToolDomain.AWS
    risk_level = ToolRiskLevel.L3_DESTRUCTIVE
    args_schema = RDSDeleteInstanceSchema
    blast_radius = "database"
    is_destructive = True
    allows_unbacked_rollback = False

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        args = self.args_schema(**kwargs)
        try:
            client = await asyncio.to_thread(_client, "rds", args.region)
            delete_kwargs: dict[str, Any] = {
                "DBInstanceIdentifier": args.db_instance_identifier,
                "SkipFinalSnapshot": args.skip_final_snapshot,
            }
            if not args.skip_final_snapshot:
                snapshot_id = (
                    args.final_db_snapshot_identifier
                    or f"{args.db_instance_identifier}-final-snapshot"
                )
                delete_kwargs["FinalDBSnapshotIdentifier"] = snapshot_id

            resp = await asyncio.to_thread(client.delete_db_instance, **delete_kwargs)
            db_inst = resp.get("DBInstance", {})
            return NexusToolResult(
                success=True,
                data={
                    "db_instance_identifier": args.db_instance_identifier,
                    "status": db_inst.get("DBInstanceStatus", "deleting"),
                },
            )
        except Exception as exc:
            return NexusToolResult(success=False, error=str(exc))
