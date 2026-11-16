"""Integration test for shop-demo CrashLoopBackOff incident diagnosis, approval gate, and rollback."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.graph.workflow import IncidentWorkflow


@pytest.mark.asyncio
async def test_shop_demo_crashloop_requires_approval_and_rolls_back():
    """Verify CrashLoopBackOff pod triggers approval gate with rollback plan, not auto-resolution."""
    events = [
        {
            "agent": "k8s",
            "signal_type": "state_transition",
            "severity": "error",
            "namespace": "default",
            "resource_name": "shop-demo-9ff49b95f-jm2hg",
            "resource_kind": "Pod",
            "context": {
                "summary": "[Pod/shop-demo-9ff49b95f-jm2hg] BackOff: Back-off restarting failed container",
            },
        }
    ]

    mock_nats = MagicMock()
    mock_nats.publish = AsyncMock()

    workflow = IncidentWorkflow(nats_client=mock_nats)

    # 1. Run incident initially - should pause at approval gate, NOT auto-resolve
    result = await workflow.run_incident(
        {"incident_id": "inc-shop-demo-test", "events": events, "severity": "error"},
        thread_id="thread-shop-demo-test",
    )

    # Must NOT be marked resolved without approval
    assert result.get("resolved") is not True

    # Must have staged pending approval
    pending = workflow.pending_approvals()
    assert len(pending) == 1
    approval = pending[0]
    assert approval["incident_id"] == "inc-shop-demo-test"
    plan = approval["plan"]
    assert "steps" in plan and len(plan["steps"]) > 0

    # The step must target the deployment shop-demo, not the ephemeral pod hash
    step = plan["steps"][0]
    assert step["tool_name"] == "k8s_rollback_deployment"
    assert step["parameters"]["deployment_name"] == "shop-demo"

    # 2. Operator approves remediation
    with patch(
        "nexus.agents.k8s_tools.rollback_deployment",
        return_value="deployment.apps/shop-demo rolled back",
    ):
        with patch(
            "nexus.agents.k8s_tools.describe_resource",
            return_value="Deployment shop-demo: replicas=2, ready=2, available=2, unavailable=0",
        ):
            resumed = await workflow.resume_incident(
                thread_id="thread-shop-demo-test", approval_decision="approved"
            )
            assert resumed.get("resolved") is True
            assert resumed.get("fsm_state") == "resolved"
