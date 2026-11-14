"""
Tier-1 incident-flow wiring tests:
  1. Verification polls until healthy instead of a single 0.5s check
  2. Govern blocks on circuit breaker / cooldown (wired from ActionLadder)
  3. Rollback uses real captured snapshots (execution record → plan step)
  4. Workflow completion records learning outcomes (audit trail + KnowledgeBase)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.graph.nodes.execute import execute_node
from nexus.graph.nodes.govern import govern_node
from nexus.graph.nodes.rollback import rollback_node
from nexus.graph.nodes.verify import verify_node
from nexus.graph.platform import HealthCheckResult, RollbackSnapshot
from nexus.graph.workflow import IncidentWorkflow


@pytest.fixture
def status_context():
    from nexus.observability.status_api import context

    saved = (
        context.action_ladder,
        context.audit_trail,
        context.knowledge_base,
    )
    yield context
    (
        context.action_ladder,
        context.audit_trail,
        context.knowledge_base,
    ) = saved


def _fake_registry(adapter: MagicMock) -> MagicMock:
    registry = MagicMock()
    registry.get.return_value = adapter
    registry.resolve_adapter.return_value = adapter
    return registry


def _target():
    from nexus.graph.platform import TargetResource

    return TargetResource(name="svc-a", namespace="default", platform="kubernetes")


def _state(**overrides):
    base = {
        "incident_id": "inc-wire-01",
        "fsm_state": "verifying",
        "platform": "kubernetes",
        "target": {"name": "svc-a", "namespace": "default", "platform": "kubernetes"},
        "events": [{"signal_type": "pod_oomkilled", "resource_name": "svc-a"}],
        "plan": {
            "steps": [
                {
                    "step_index": 0,
                    "tool_name": "k8s_restart_deployment",
                    "parameters": {"namespace": "default", "deployment_name": "svc-a"},
                }
            ]
        },
        "execution_records": [
            {"step_index": 0, "tool_name": "k8s_restart_deployment", "success": True}
        ],
        "retry_count": 0,
        "max_retries": 2,
    }
    base.update(overrides)
    return base


# ── 1. Verification polling ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_polls_until_healthy():
    """Unhealthy-then-healthy target resolves within the poll window (no retry/rollback)."""
    adapter = MagicMock()
    adapter.verify_health = AsyncMock(
        side_effect=[
            HealthCheckResult(
                healthy=False, slo_restored=False, failure_reason="restarting"
            ),
            HealthCheckResult(
                healthy=False, slo_restored=False, failure_reason="restarting"
            ),
            HealthCheckResult(healthy=True, slo_restored=True, details="Ready 1/1"),
        ]
    )

    with (
        patch(
            "nexus.graph.nodes.verify.get_platform_registry",
            return_value=_fake_registry(adapter),
        ),
        patch("nexus.graph.nodes.verify.VERIFY_TIMEOUT_S", 2.0),
        patch("nexus.graph.nodes.verify.VERIFY_POLL_INTERVAL_S", 0.01),
    ):
        result = await verify_node(_state())

    assert result["resolved"] is True
    assert result["verification"]["healthy"] is True
    assert adapter.verify_health.await_count == 3


@pytest.mark.asyncio
async def test_verify_failure_after_timeout_records_circuit_breaker():
    """Perpetually unhealthy target exhausts the poll window, records a CB failure, rolls back."""
    adapter = MagicMock()
    adapter.verify_health = AsyncMock(
        return_value=HealthCheckResult(
            healthy=False, slo_restored=False, failure_reason="OOM persists"
        )
    )
    ladder = MagicMock()
    ladder.record_post_check_failure = MagicMock()
    ladder.set_cooldown = AsyncMock()

    from nexus.observability.status_api import context

    context.action_ladder = ladder
    try:
        with (
            patch(
                "nexus.graph.nodes.verify.get_platform_registry",
                return_value=_fake_registry(adapter),
            ),
            patch("nexus.graph.nodes.verify.VERIFY_TIMEOUT_S", 0.05),
            patch("nexus.graph.nodes.verify.VERIFY_POLL_INTERVAL_S", 0.02),
        ):
            result = await verify_node(_state(max_retries=0))
    finally:
        context.action_ladder = None

    assert result.get("resolved") is not True
    assert result["fsm_state"] == "rolling_back"
    ladder.record_post_check_failure.assert_called_once()
    ladder.set_cooldown.assert_not_awaited()


@pytest.mark.asyncio
async def test_verify_success_records_cb_and_sets_cooldown():
    """Healthy verification resets the circuit breaker and sets a cooldown for the action+target."""
    adapter = MagicMock()
    adapter.verify_health = AsyncMock(
        return_value=HealthCheckResult(healthy=True, slo_restored=True, details="Ready")
    )
    ladder = MagicMock()
    ladder.record_post_check_success = MagicMock()
    ladder.set_cooldown = AsyncMock()

    from nexus.observability.status_api import context

    context.action_ladder = ladder
    try:
        with patch(
            "nexus.graph.nodes.verify.get_platform_registry",
            return_value=_fake_registry(adapter),
        ):
            result = await verify_node(_state())
    finally:
        context.action_ladder = None

    assert result["resolved"] is True
    ladder.record_post_check_success.assert_called_once()
    ladder.set_cooldown.assert_awaited_once_with(
        "k8s_restart_deployment", "default/svc-a"
    )


# ── 2. Governance circuit breaker + cooldown ─────────────────────────────────


def _ladder(cb_open: bool, in_cooldown: bool) -> MagicMock:
    ladder = MagicMock()
    ladder.governance_cb.is_open = cb_open
    ladder.governance_cb.consecutive_failures = 3 if cb_open else 0
    ladder.cooldown_store.is_in_cooldown = AsyncMock(return_value=in_cooldown)
    ladder.cooldown_store.remaining_seconds = AsyncMock(return_value=250.0)
    return ladder


@pytest.mark.asyncio
async def test_govern_blocks_when_circuit_breaker_open(status_context):
    status_context.action_ladder = _ladder(cb_open=True, in_cooldown=False)

    result = await govern_node(_state(platform="aws", fsm_state="planning"))

    assert result["governance"]["allowed"] is False
    assert result["governance"]["circuit_breaker_open"] is True
    assert any("circuit breaker" in r for r in result["governance"]["reasons"])


@pytest.mark.asyncio
async def test_govern_blocks_when_target_in_cooldown(status_context):
    status_context.action_ladder = _ladder(cb_open=False, in_cooldown=True)

    result = await govern_node(_state(platform="aws", fsm_state="planning"))

    assert result["governance"]["allowed"] is False
    assert result["governance"]["cooldown_active"] is True
    assert any("cooldown" in r for r in result["governance"]["reasons"])


@pytest.mark.asyncio
async def test_govern_passes_when_safety_clear(status_context):
    status_context.action_ladder = _ladder(cb_open=False, in_cooldown=False)

    result = await govern_node(
        _state(platform="aws", fsm_state="planning", approval_decision="approved")
    )

    assert result["governance"]["allowed"] is True
    assert result["governance"]["circuit_breaker_open"] is False
    assert result["governance"]["cooldown_active"] is False


# ── 3. Snapshot-based rollback ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_stores_snapshot_rollback_fields():
    """execute_node persists the captured snapshot's real inverse into the execution record."""
    adapter = MagicMock()
    adapter.capture_snapshot = AsyncMock(
        return_value=RollbackSnapshot(
            snapshot_id="snap-1",
            target=_target(),
            action_name="k8s_scale_deployment",
            rollback_tool="k8s_scale_deployment",
            rollback_parameters={
                "namespace": "default",
                "deployment_name": "svc-a",
                "replicas": 3,
            },
        )
    )
    adapter.execute_action = AsyncMock(
        return_value=MagicMock(success=True, data={}, error=None)
    )

    with patch(
        "nexus.graph.nodes.execute.get_platform_registry",
        return_value=_fake_registry(adapter),
    ):
        result = await execute_node(_state())

    rec = result["execution_records"][0]
    assert rec["rollback_tool"] == "k8s_scale_deployment"
    assert rec["rollback_parameters"]["replicas"] == 3


@pytest.mark.asyncio
async def test_rollback_prefers_captured_snapshot_over_hardcoded_inverse():
    """rollback_node uses the execution record's captured inverse, not 'restart deployment'."""
    adapter = MagicMock()
    adapter.execute_rollback = AsyncMock(
        return_value=MagicMock(success=True, data={}, error=None)
    )

    state = _state(
        execution_records=[
            {
                "step_index": 0,
                "tool_name": "k8s_scale_deployment",
                "success": True,
                "snapshot_id": "snap-1",
                "rollback_tool": "k8s_scale_deployment",
                "rollback_parameters": {
                    "namespace": "default",
                    "deployment_name": "svc-a",
                    "replicas": 3,
                },
            }
        ]
    )

    with patch(
        "nexus.graph.nodes.rollback.get_platform_registry",
        return_value=_fake_registry(adapter),
    ):
        await rollback_node(state)

    snap = adapter.execute_rollback.await_args.args[0]
    assert snap.rollback_tool == "k8s_scale_deployment"
    assert snap.rollback_parameters["replicas"] == 3


@pytest.mark.asyncio
async def test_rollback_falls_back_to_plan_step_fields():
    """Without a captured snapshot, the plan step's deterministic rollback fields are used."""
    adapter = MagicMock()
    adapter.execute_rollback = AsyncMock(
        return_value=MagicMock(success=True, data={}, error=None)
    )

    state = _state(
        plan={
            "steps": [
                {
                    "step_index": 0,
                    "tool_name": "k8s_scale_deployment",
                    "rollback_tool": "k8s_scale_deployment",
                    "rollback_parameters": {
                        "namespace": "default",
                        "deployment_name": "svc-a",
                        "replicas": 2,
                    },
                }
            ]
        },
        execution_records=[
            {"step_index": 0, "tool_name": "k8s_scale_deployment", "success": True}
        ],
    )

    with patch(
        "nexus.graph.nodes.rollback.get_platform_registry",
        return_value=_fake_registry(adapter),
    ):
        await rollback_node(state)

    snap = adapter.execute_rollback.await_args.args[0]
    assert snap.rollback_tool == "k8s_scale_deployment"
    assert snap.rollback_parameters["replicas"] == 2


# ── 4. Learning-plane recording at workflow completion ────────────────────────


def _completion_result(resolved: bool, rolled_back: bool = False) -> dict:
    return {
        "incident_id": "inc-learn-01",
        "fsm_state": "resolved" if resolved else "escalated",
        "resolved": resolved,
        "escalated": not resolved,
        "rollback_executed": rolled_back,
        "target": {"name": "svc-a", "namespace": "default", "platform": "kubernetes"},
        "events": [
            {
                "signal_type": "pod_oomkilled",
                "resource_name": "svc-a",
                "namespace": "default",
            }
        ],
        "diagnosis": {"failure_class": "resource_exhaustion", "confidence": 0.9},
        "plan": {
            "failure_mode": "resource_exhaustion",
            "steps": [
                {"tool_name": "k8s_restart_deployment", "description": "restart"}
            ],
        },
        "verification": {
            "healthy": resolved,
            "slo_restored": resolved,
            "details": "ok",
        },
        "execution_records": [
            {"step_index": 0, "tool_name": "k8s_restart_deployment", "success": True}
        ],
        "error_message": None if resolved else "unhealthy after retries",
    }


@pytest.mark.asyncio
async def test_completion_records_success_outcome_to_learning_plane(status_context):
    workflow = IncidentWorkflow()
    status_context.audit_trail = MagicMock()
    status_context.audit_trail.write = AsyncMock(return_value="audit-1")
    status_context.knowledge_base = MagicMock()
    status_context.knowledge_base.record_incident_outcome = AsyncMock()
    status_context.knowledge_base.record_pattern = AsyncMock()

    await workflow._handle_workflow_completion(
        _completion_result(resolved=True), inc_id="inc-learn-01", thread_id="t1"
    )

    status_context.audit_trail.write.assert_awaited_once()
    kwargs = status_context.audit_trail.write.await_args.kwargs
    assert kwargs["execution_outcome"] == "success"
    assert kwargs["runbook_id"] == "resource_exhaustion"
    assert kwargs["incident_id"] == "inc-learn-01"

    status_context.knowledge_base.record_incident_outcome.assert_awaited_once()
    io_kwargs = status_context.knowledge_base.record_incident_outcome.await_args.kwargs
    assert io_kwargs["resolved"] is True
    assert io_kwargs["action_type"] == "k8s_restart_deployment"

    status_context.knowledge_base.record_pattern.assert_awaited_once()
    p_kwargs = status_context.knowledge_base.record_pattern.await_args.kwargs
    assert p_kwargs["signal_types"] == {"pod_oomkilled"}
    assert p_kwargs["success"] is True


@pytest.mark.asyncio
async def test_completion_records_rolled_back_outcome(status_context):
    workflow = IncidentWorkflow()
    status_context.audit_trail = MagicMock()
    status_context.audit_trail.write = AsyncMock(return_value="audit-2")
    status_context.knowledge_base = MagicMock()
    status_context.knowledge_base.record_incident_outcome = AsyncMock()
    status_context.knowledge_base.record_pattern = AsyncMock()

    await workflow._handle_workflow_completion(
        _completion_result(resolved=False, rolled_back=True),
        inc_id="inc-learn-02",
        thread_id="t2",
    )

    kwargs = status_context.audit_trail.write.await_args.kwargs
    assert kwargs["execution_outcome"] == "rolled_back"
    assert kwargs["rollback_triggered"] is True
    status_context.knowledge_base.record_pattern.assert_awaited_once()
    assert (
        status_context.knowledge_base.record_pattern.await_args.kwargs["success"]
        is False
    )
