from unittest.mock import patch

import pytest

from nexus.db.postgres import SQLiteFallbackClient
from nexus.tools.base import ToolDomain, ToolRiskLevel
from nexus.tools.registry import ToolRegistry


def test_tool_registry_registration_and_filtering():
    reg = ToolRegistry()

    # Total tools registered
    all_tools = reg.list_tools()
    assert len(all_tools) >= 10

    # Domain filtering
    k8s_tools = reg.list_tools(domain=ToolDomain.K8S)
    assert any(t.name == "k8s_restart_deployment" for t in k8s_tools)
    assert not any(t.domain == ToolDomain.AWS for t in k8s_tools)

    aws_tools = reg.list_tools(domain=ToolDomain.AWS)
    assert any(t.name == "aws_update_lambda_memory" for t in aws_tools)
    assert not any(t.domain == ToolDomain.K8S for t in aws_tools)

    # Risk level filtering (read only)
    l0_tools = reg.list_tools(max_risk=ToolRiskLevel.L0_OBSERVE)
    assert all(t.risk_level == ToolRiskLevel.L0_OBSERVE for t in l0_tools)
    assert any(t.name == "k8s_get_pod_logs" for t in l0_tools)
    assert not any(t.name == "k8s_rollback_deployment" for t in l0_tools)


def test_tool_declarations_schema():
    reg = ToolRegistry()
    decls = reg.get_function_declarations(domain=ToolDomain.K8S)
    assert len(decls) > 0
    restart_tool = next(d for d in decls if d["name"] == "k8s_restart_deployment")
    assert "parameters" in restart_tool
    assert "properties" in restart_tool["parameters"]
    assert "deployment_name" in restart_tool["parameters"]["properties"]


@pytest.mark.asyncio
async def test_tool_execution_with_db_logging():
    db = SQLiteFallbackClient(db_path=":memory:")
    await db.initialize()
    inc_id = await db.create_incident(
        fingerprint="fp-test",
        environment="kubernetes",
        target_resource="default/my-app",
    )
    run_id = await db.start_agent_run(inc_id, "orchestrator", "gemini-3.1-flash-lite")

    reg = ToolRegistry()

    # Mock the internal k8s_tools call for restart_deployment
    with patch("nexus.agents.k8s_tools.restart_deployment", return_value="Restarted successfully"):
        res = await reg.execute_tool(
            name="k8s_restart_deployment",
            parameters={"namespace": "default", "deployment_name": "my-app"},
            run_id=run_id,
            db_client=db,
        )
        assert res.success is True
        assert res.data["message"] == "Restarted successfully"

    # Verify tool call was logged to DB
    trace = await db.get_incident_trace(inc_id)
    tool_calls = trace["agent_runs"][0]["tool_calls"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["tool_name"] == "k8s_restart_deployment"
    assert tool_calls[0]["status"] == "success"

    await db.close()
