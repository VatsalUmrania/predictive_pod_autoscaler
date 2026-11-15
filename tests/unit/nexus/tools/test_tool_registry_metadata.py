"""
Unit tests for NEXUS Tool Registry and immutable safety metadata.
"""

from __future__ import annotations

import pytest
from langchain_core.tools import StructuredTool

from nexus.tools.base import ToolDomain, ToolRiskLevel
from nexus.tools.registry import ToolRegistry, default_registry


def test_default_registry_all_tools_have_immutable_metadata():
    """Verify all registered tools define immutable safety attributes."""
    tools = default_registry.list_tools()
    assert len(tools) >= 15, f"Expected at least 15 default tools, found {len(tools)}"

    for tool in tools:
        assert hasattr(tool, "blast_radius"), f"Tool {tool.name} missing blast_radius"
        assert isinstance(tool.blast_radius, str)
        assert hasattr(
            tool, "is_destructive"
        ), f"Tool {tool.name} missing is_destructive"
        assert isinstance(tool.is_destructive, bool)
        assert hasattr(
            tool, "allows_unbacked_rollback"
        ), f"Tool {tool.name} missing allows_unbacked_rollback"
        assert isinstance(tool.allows_unbacked_rollback, bool)


def test_critical_tool_safety_attributes():
    """Verify specific safety metadata for critical operations."""
    # 1. DynamoDB Update Table (Mutating, safe rollback needed)
    ddb_update = default_registry.get("aws_dynamodb_update_table")
    assert ddb_update is not None
    assert ddb_update.domain == ToolDomain.AWS
    assert ddb_update.risk_level == ToolRiskLevel.L2_MUTATE_APPROVAL
    assert ddb_update.blast_radius == "service_wide"
    assert ddb_update.is_destructive is False
    assert ddb_update.allows_unbacked_rollback is False

    # 2. DynamoDB Describe Table (Read-only observation)
    ddb_desc = default_registry.get("aws_dynamodb_describe_table")
    assert ddb_desc is not None
    assert ddb_desc.domain == ToolDomain.AWS
    assert ddb_desc.risk_level == ToolRiskLevel.L0_OBSERVE
    assert ddb_desc.blast_radius == "none"
    assert ddb_desc.is_destructive is False

    # 3. RDS Delete Instance (Destructive datastore mutation)
    rds_delete = default_registry.get("aws_rds_delete_db_instance")
    assert rds_delete is not None
    assert rds_delete.domain == ToolDomain.AWS
    assert rds_delete.risk_level == ToolRiskLevel.L3_DESTRUCTIVE
    assert rds_delete.blast_radius == "database"
    assert rds_delete.is_destructive is True
    assert rds_delete.allows_unbacked_rollback is False

    # 4. K8s Restart Deployment (Safe automated with unbacked rollback)
    k8s_restart = default_registry.get("k8s_restart_deployment")
    assert k8s_restart is not None
    assert k8s_restart.domain == ToolDomain.K8S
    assert k8s_restart.risk_level == ToolRiskLevel.L1_SAFE
    assert k8s_restart.blast_radius == "pod"
    assert k8s_restart.is_destructive is False
    assert k8s_restart.allows_unbacked_rollback is True


def test_diagnostic_and_remediation_category_filters():
    """Verify get_diagnostic_tools() and get_remediation_tools() filtering."""
    registry = ToolRegistry()

    diagnostic_tools = registry.get_diagnostic_tools()
    assert len(diagnostic_tools) > 0
    for tool in diagnostic_tools:
        assert (
            tool.risk_level == ToolRiskLevel.L0_OBSERVE
        ), f"Tool {tool.name} in diagnostic but not L0_OBSERVE"

    remediation_tools = registry.get_remediation_tools()
    assert len(remediation_tools) > 0
    for tool in remediation_tools:
        assert tool.risk_level in (
            ToolRiskLevel.L1_SAFE,
            ToolRiskLevel.L2_MUTATE_APPROVAL,
            ToolRiskLevel.L3_DESTRUCTIVE,
        ), f"Tool {tool.name} in remediation has unexpected risk level {tool.risk_level}"

    # Verify partition property (no overlap and complete coverage)
    total_tools = registry.list_tools()
    assert len(diagnostic_tools) + len(remediation_tools) == len(total_tools)


def test_as_langchain_tool_conversion():
    """Verify .as_langchain_tool() returns a valid StructuredTool with metadata."""
    tool = default_registry.get("aws_dynamodb_describe_table")
    assert tool is not None

    lc_tool = tool.as_langchain_tool()
    assert isinstance(lc_tool, StructuredTool)
    assert lc_tool.name == "aws_dynamodb_describe_table"
    assert "[AWS | L0_OBSERVE]" in lc_tool.description
    assert lc_tool.metadata["blast_radius"] == "none"
    assert lc_tool.metadata["is_destructive"] is False
    assert lc_tool.metadata["risk_level"] == "L0_OBSERVE"


def test_registry_filters_as_langchain():
    """Verify get_diagnostic_tools(as_langchain=True) returns StructuredTools."""
    registry = ToolRegistry()
    lc_diagnostic = registry.get_diagnostic_tools(as_langchain=True)
    assert len(lc_diagnostic) > 0
    for t in lc_diagnostic:
        assert isinstance(t, StructuredTool)

    lc_remediation = registry.get_remediation_tools(as_langchain=True)
    assert len(lc_remediation) > 0
    for t in lc_remediation:
        assert isinstance(t, StructuredTool)


@pytest.mark.asyncio
async def test_langchain_tool_invocation(monkeypatch):
    """Verify that a converted LangChain tool can be invoked synchronously and asynchronously."""
    tool = default_registry.get("aws_dynamodb_describe_table")
    assert tool is not None

    # Mock _client
    class FakeDynamoDB:
        def describe_table(self, **kwargs):
            table_name = kwargs.get("TableName")
            return {
                "Table": {
                    "TableName": table_name,
                    "TableStatus": "ACTIVE",
                    "ItemCount": 42,
                    "TableSizeBytes": 1024,
                }
            }

    import nexus.tools.aws.observe_tools as obs_module

    monkeypatch.setattr(obs_module, "_client", lambda s, r=None: FakeDynamoDB())

    lc_tool = tool.as_langchain_tool()

    # Async invocation
    res_async = await lc_tool.ainvoke({"table_name": "test-table"})
    assert res_async["success"] is True
    assert res_async["data"]["table_name"] == "test-table"
    assert res_async["data"]["status"] == "ACTIVE"

    # Sync invocation
    res_sync = lc_tool.invoke({"table_name": "test-table"})
    assert res_sync["success"] is True
    assert res_sync["data"]["table_name"] == "test-table"
