"""
AWS DynamoDB and RDS Mutating Remediation Tools
"""

from __future__ import annotations

import asyncio
from typing import Any

from nexus.engine.tools.aws.client import get_boto3_client
from nexus.engine.tools.aws.schemas import (
    DynamoDBUpdateTableSchema,
    RDSDeleteInstanceSchema,
)
from nexus.engine.tools.base import (
    NexusTool,
    NexusToolResult,
    ToolDomain,
    ToolRiskLevel,
)


def _client(service_name: str, region: str | None = None):
    import sys

    mut_mod = sys.modules.get("nexus.engine.tools.aws.mutate_tools")
    if mut_mod and hasattr(mut_mod, "_client"):
        return mut_mod._client(service_name, region)
    return get_boto3_client(service_name, region)


class DynamoDBUpdateTableTool(NexusTool):
    name = "aws_dynamodb_update_table"
    description = "Update DynamoDB table provisioned throughput or billing mode."
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
            curr_tp = table_info.get("ProvisionedThroughput", {})
            curr_rcu, curr_wcu = curr_tp.get("ReadCapacityUnits"), curr_tp.get(
                "WriteCapacityUnits"
            )
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
        prev_wcu, prev_rcu, prev_billing = (
            kwargs.get("previous_write_capacity_units"),
            kwargs.get("previous_read_capacity_units"),
            kwargs.get("previous_billing_mode"),
        )
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
            return {"tool_name": self.name, "parameters": params}
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
                delete_kwargs["FinalDBSnapshotIdentifier"] = (
                    args.final_db_snapshot_identifier
                    or f"{args.db_instance_identifier}-final-snapshot"
                )
            resp = await asyncio.to_thread(client.delete_db_instance, **delete_kwargs)
            return NexusToolResult(
                success=True,
                data={
                    "db_instance_identifier": args.db_instance_identifier,
                    "status": resp.get("DBInstance", {}).get(
                        "DBInstanceStatus", "deleting"
                    ),
                },
            )
        except Exception as exc:
            return NexusToolResult(success=False, error=str(exc))
