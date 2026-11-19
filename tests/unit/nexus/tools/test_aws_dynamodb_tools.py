"""
Unit tests for NEXUS AWS DynamoDB and RDS tools.
"""

from __future__ import annotations

import pytest

from nexus.engine.tools.aws.mutate.mutate_tools import (
    DynamoDBUpdateTableTool,
    RDSDeleteInstanceTool,
)
from nexus.engine.tools.aws.observe.observe_tools import DynamoDBDescribeTableTool
from nexus.engine.tools.base import ToolRiskLevel


@pytest.fixture
def fake_dynamodb():
    class FakeDynamoDBClient:
        def __init__(self):
            self.tables = {
                "orders": {
                    "TableName": "orders",
                    "TableStatus": "ACTIVE",
                    "ItemCount": 100,
                    "TableSizeBytes": 5000,
                    "ProvisionedThroughput": {
                        "ReadCapacityUnits": 50,
                        "WriteCapacityUnits": 50,
                    },
                    "BillingModeSummary": {
                        "BillingMode": "PROVISIONED",
                    },
                }
            }
            self.updated_calls = []

        def describe_table(self, **kwargs):
            table_name = kwargs.get("TableName")
            if table_name not in self.tables:
                raise Exception(
                    f"ResourceNotFoundException: Table {table_name} not found"
                )
            return {"Table": self.tables[table_name]}

        def update_table(self, **kwargs):
            table_name = kwargs["TableName"]
            if table_name not in self.tables:
                raise Exception(
                    f"ResourceNotFoundException: Table {table_name} not found"
                )
            self.updated_calls.append(kwargs)
            if "ProvisionedThroughput" in kwargs:
                self.tables[table_name]["ProvisionedThroughput"] = kwargs[
                    "ProvisionedThroughput"
                ]
            if "BillingMode" in kwargs:
                self.tables[table_name]["BillingModeSummary"] = {
                    "BillingMode": kwargs["BillingMode"]
                }
            return {"TableDescription": self.tables[table_name]}

    return FakeDynamoDBClient()


@pytest.fixture
def fake_rds():
    class FakeRDSClient:
        def __init__(self):
            self.instances = {
                "prod-db": {
                    "DBInstanceIdentifier": "prod-db",
                    "DBInstanceStatus": "available",
                }
            }
            self.deleted_calls = []

        def delete_db_instance(self, **kwargs):
            db_id = kwargs["DBInstanceIdentifier"]
            if db_id not in self.instances:
                raise Exception(f"DBInstanceNotFound: DB instance {db_id} not found")
            self.deleted_calls.append(kwargs)
            self.instances[db_id]["DBInstanceStatus"] = "deleting"
            return {"DBInstance": self.instances[db_id]}

    return FakeRDSClient()


@pytest.mark.asyncio
async def test_dynamodb_describe_table_success(fake_dynamodb, monkeypatch):
    """Test successful describe_table observation."""
    import nexus.engine.tools.aws.observe.observe_tools as obs_module

    monkeypatch.setattr(obs_module, "_client", lambda s, r=None: fake_dynamodb)

    tool = DynamoDBDescribeTableTool()
    assert tool.name == "aws_dynamodb_describe_table"
    assert tool.risk_level == ToolRiskLevel.L0_OBSERVE
    assert tool.blast_radius == "none"

    result = await tool.execute(table_name="orders")
    assert result.success is True
    assert result.data["table_name"] == "orders"
    assert result.data["status"] == "ACTIVE"
    assert result.data["provisioned_throughput"]["WriteCapacityUnits"] == 50


@pytest.mark.asyncio
async def test_dynamodb_describe_table_not_found(fake_dynamodb, monkeypatch):
    """Test describe_table failure when table does not exist."""
    import nexus.engine.tools.aws.observe.observe_tools as obs_module

    monkeypatch.setattr(obs_module, "_client", lambda s, r=None: fake_dynamodb)

    tool = DynamoDBDescribeTableTool()
    result = await tool.execute(table_name="nonexistent")
    assert result.success is False
    assert "ResourceNotFoundException" in result.error


@pytest.mark.asyncio
async def test_dynamodb_update_table_provisioned_capacity(fake_dynamodb, monkeypatch):
    """Test updating DynamoDB write capacity units (WCU scaling)."""
    import nexus.engine.tools.aws.mutate.mutate_tools as mut_module

    monkeypatch.setattr(mut_module, "_client", lambda s, r=None: fake_dynamodb)

    tool = DynamoDBUpdateTableTool()
    assert tool.name == "aws_dynamodb_update_table"
    assert tool.risk_level == ToolRiskLevel.L2_MUTATE_APPROVAL
    assert tool.blast_radius == "service_wide"
    assert tool.is_destructive is False
    assert tool.allows_unbacked_rollback is False

    result = await tool.execute(table_name="orders", write_capacity_units=250)
    assert result.success is True
    assert result.data["table_name"] == "orders"
    assert result.data["pre"]["write_capacity_units"] == 50
    assert result.data["post"]["write_capacity_units"] == 250
    assert (
        fake_dynamodb.tables["orders"]["ProvisionedThroughput"]["WriteCapacityUnits"]
        == 250
    )


@pytest.mark.asyncio
async def test_dynamodb_update_table_billing_mode_on_demand(fake_dynamodb, monkeypatch):
    """Test updating DynamoDB billing mode to PAY_PER_REQUEST."""
    import nexus.engine.tools.aws.mutate.mutate_tools as mut_module

    monkeypatch.setattr(mut_module, "_client", lambda s, r=None: fake_dynamodb)

    tool = DynamoDBUpdateTableTool()
    result = await tool.execute(table_name="orders", billing_mode="PAY_PER_REQUEST")
    assert result.success is True
    assert result.data["post"]["billing_mode"] == "PAY_PER_REQUEST"


def test_dynamodb_update_table_rollback_action():
    """Test generating the inverse rollback action for DynamoDB throughput update."""
    tool = DynamoDBUpdateTableTool()

    # Rollback with previous WCU
    rollback = tool.get_rollback_action(
        table_name="orders",
        previous_write_capacity_units=50,
        region="us-east-1",
    )
    assert rollback is not None
    assert rollback["tool_name"] == "aws_dynamodb_update_table"
    assert rollback["parameters"]["table_name"] == "orders"
    assert rollback["parameters"]["write_capacity_units"] == 50
    assert rollback["parameters"]["region"] == "us-east-1"

    # None if no previous state provided
    assert tool.get_rollback_action(table_name="orders") is None


@pytest.mark.asyncio
async def test_rds_delete_instance_success(fake_rds, monkeypatch):
    """Test deleting an RDS DB instance (destructive)."""
    import nexus.engine.tools.aws.mutate.mutate_tools as mut_module

    monkeypatch.setattr(mut_module, "_client", lambda s, r=None: fake_rds)

    tool = RDSDeleteInstanceTool()
    assert tool.name == "aws_rds_delete_db_instance"
    assert tool.risk_level == ToolRiskLevel.L3_DESTRUCTIVE
    assert tool.blast_radius == "database"
    assert tool.is_destructive is True

    result = await tool.execute(
        db_instance_identifier="prod-db", skip_final_snapshot=True
    )
    assert result.success is True
    assert result.data["db_instance_identifier"] == "prod-db"
    assert result.data["status"] == "deleting"
    assert fake_rds.instances["prod-db"]["DBInstanceStatus"] == "deleting"
    assert len(fake_rds.deleted_calls) == 1
    assert fake_rds.deleted_calls[0]["SkipFinalSnapshot"] is True


@pytest.mark.asyncio
async def test_rds_delete_instance_with_snapshot(fake_rds, monkeypatch):
    """Test deleting an RDS instance creating a final snapshot."""
    import nexus.engine.tools.aws.mutate.mutate_tools as mut_module

    monkeypatch.setattr(mut_module, "_client", lambda s, r=None: fake_rds)

    tool = RDSDeleteInstanceTool()
    result = await tool.execute(
        db_instance_identifier="prod-db",
        skip_final_snapshot=False,
        final_db_snapshot_identifier="custom-snapshot-1",
    )
    assert result.success is True
    assert fake_rds.deleted_calls[0]["FinalDBSnapshotIdentifier"] == "custom-snapshot-1"


@pytest.mark.asyncio
async def test_rds_delete_instance_not_found(fake_rds, monkeypatch):
    """Test error handling when RDS instance does not exist."""
    import nexus.engine.tools.aws.mutate.mutate_tools as mut_module

    monkeypatch.setattr(mut_module, "_client", lambda s, r=None: fake_rds)

    tool = RDSDeleteInstanceTool()
    result = await tool.execute(db_instance_identifier="missing-db")
    assert result.success is False
    assert "DBInstanceNotFound" in result.error
