"""
Unit tests for unified ToolRegistry read/write separation and schema extraction.
"""

from __future__ import annotations

import pytest

from nexus.engine.tools.registry import ToolRegistry


def test_tool_registry_read_only_and_mutating_separation():
    reg = ToolRegistry()
    read_tools = reg.get_read_only_tools_as_langchain()
    read_names = {t.name for t in read_tools}

    # Universal read tools must be present
    assert "aws_inspect_resource" in read_names
    assert "aws_query_telemetry" in read_names
    assert "k8s_inspect_resource" in read_names
    assert "k8s_get_logs" in read_names

    # Mutating tools must NOT be present in read-only list
    assert "aws_mutate_resource" not in read_names
    assert "k8s_mutate_resource" not in read_names

    # Mutating schemas must be present
    mutating_schemas = reg.get_mutating_tool_schemas()
    mut_names = {s["name"] for s in mutating_schemas}
    assert "aws_mutate_resource" in mut_names
    assert "k8s_mutate_resource" in mut_names
    assert "aws_inspect_resource" not in mut_names


@pytest.mark.asyncio
async def test_tool_registry_execution_boundaries():
    reg = ToolRegistry()

    # Attempting to execute mutating tool via execute_read_only must raise PermissionError
    with pytest.raises(
        PermissionError, match="cannot be called during read-only inspection"
    ):
        await reg.execute_read_only(
            "aws_mutate_resource",
            {
                "service": "lambda",
                "operation": "update_function",
                "parameters": {},
                "target_arn": "arn:xxx",
            },
        )

    # Calling unknown read-only tool raises KeyError
    with pytest.raises(KeyError):
        await reg.execute_read_only("nonexistent_tool", {})


def test_tool_registry_mcp_loader():
    reg = ToolRegistry()
    loaded = reg.load_mcp_tools([{"name": "aws-mcp"}, {"name": "k8s-mcp"}])
    assert loaded == 2
