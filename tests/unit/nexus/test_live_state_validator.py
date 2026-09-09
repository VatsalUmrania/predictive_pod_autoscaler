"""
Tests for nexus.governance.live_state_validator — check_live_state().

Uses unittest.mock to simulate Kubernetes API responses without a real cluster.
All async tests use pytest-asyncio.

Covers:
  - restart_pod / restart_deployment: pod phase/condition checks
  - kubectl_rollout_undo / rollback_deployment: deployment availability checks
  - scale_deployment: deployment availability as scale-condition proxy
  - emit_alert / patch_annotation: always-proceed zero-blast actions
  - API failure → verdict=unknown (fail-open)
  - No pods found → verdict=proceed (cannot confirm, err safe)
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from nexus.governance.live_state_validator import (
    LiveStateReport,
    check_live_state,
)

pytestmark = pytest.mark.asyncio


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_pod(
    name: str,
    phase: str = "Running",
    ready: bool = True,
    restart_count: int = 0,
    waiting_reason: str | None = None,
    oomkilled: bool = False,
    owner_name: str = "shop-demo",
) -> MagicMock:
    """Build a mock V1Pod with the given state."""
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.owner_references = [
        MagicMock(kind="ReplicaSet", name=f"{owner_name}-abc123")
    ]
    pod.status.phase = phase
    cs = MagicMock()
    cs.ready = ready
    cs.restart_count = restart_count
    # Waiting state
    if waiting_reason:
        cs.state = MagicMock()
        cs.state.waiting = MagicMock()
        cs.state.waiting.reason = waiting_reason
    else:
        cs.state = MagicMock()
        cs.state.waiting = None
    # Last state (OOMKilled)
    if oomkilled:
        cs.last_state = MagicMock()
        cs.last_state.terminated = MagicMock()
        cs.last_state.terminated.reason = "OOMKilled"
    else:
        cs.last_state = MagicMock()
        cs.last_state.terminated = None
    pod.status.container_statuses = [cs]
    return pod


def _pod_list(*pods: MagicMock) -> MagicMock:
    result = MagicMock()
    result.items = list(pods)
    return result


def _deployment(
    desired: int = 2,
    available: int = 2,
    ready: int = 2,
    unavailable: int = 0,
) -> MagicMock:
    dep = MagicMock()
    dep.spec.replicas = desired
    dep.status.available_replicas = available
    dep.status.ready_replicas = ready
    dep.status.unavailable_replicas = unavailable
    return dep


def _k8s_core(pods=None) -> MagicMock:
    core = MagicMock()
    core.list_namespaced_pod.return_value = pods or _pod_list()
    return core


def _k8s_apps(deployment=None) -> MagicMock:
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = deployment or _deployment()
    return apps


# ── Always-proceed actions ────────────────────────────────────────────────────

class TestAlwaysProceedActions:
    async def test_emit_alert_always_proceeds(self):
        report = await check_live_state(
            action_type="emit_alert",
            namespace="default",
            resource_name="shop-demo",
            k8s_core=MagicMock(),
            k8s_apps=MagicMock(),
        )
        assert report.verdict == "proceed"
        assert "zero blast-radius" in report.evidence.get("reason", "")

    async def test_patch_annotation_always_proceeds(self):
        report = await check_live_state(
            action_type="patch_annotation",
            namespace="default",
            resource_name="shop-demo",
            k8s_core=MagicMock(),
            k8s_apps=MagicMock(),
        )
        assert report.verdict == "proceed"

    async def test_patch_configmap_always_proceeds(self):
        report = await check_live_state(
            action_type="patch_configmap",
            namespace="default",
            resource_name="shop-demo",
            k8s_core=MagicMock(),
            k8s_apps=MagicMock(),
        )
        assert report.verdict == "proceed"


# ── restart_pod / restart_deployment ─────────────────────────────────────────

class TestRestartPodCondition:
    async def test_all_pods_healthy_returns_self_healed(self):
        """All pods Running/Ready/restarts=0 → self_healed."""
        pods = _pod_list(
            _make_pod("shop-demo-abc-1", phase="Running", ready=True, restart_count=0),
            _make_pod("shop-demo-abc-2", phase="Running", ready=True, restart_count=0),
        )
        report = await check_live_state(
            action_type="restart_pod",
            namespace="default",
            resource_name="shop-demo",
            k8s_core=_k8s_core(pods),
            k8s_apps=MagicMock(),
        )
        assert report.verdict == "self_healed"
        assert report.condition_still_active is False
        assert report.evidence.get("all_running_ready") is True

    async def test_pod_in_crashloopbackoff_returns_proceed(self):
        """Pod still in CrashLoopBackOff → proceed."""
        pods = _pod_list(
            _make_pod(
                "shop-demo-abc-1",
                phase="Running",
                ready=False,
                restart_count=5,
                waiting_reason="CrashLoopBackOff",
            )
        )
        report = await check_live_state(
            action_type="restart_pod",
            namespace="default",
            resource_name="shop-demo",
            k8s_core=_k8s_core(pods),
            k8s_apps=MagicMock(),
        )
        assert report.verdict == "proceed"
        assert report.condition_still_active is True
        failing = report.evidence.get("failing_pods", [])
        assert any(p["failure_reason"] == "CrashLoopBackOff" for p in failing)

    async def test_pod_oomkilled_returns_proceed(self):
        """Pod with OOMKilled last state → proceed."""
        pods = _pod_list(
            _make_pod("shop-demo-abc-1", oomkilled=True, restart_count=2)
        )
        report = await check_live_state(
            action_type="restart_pod",
            namespace="default",
            resource_name="shop-demo",
            k8s_core=_k8s_core(pods),
            k8s_apps=MagicMock(),
        )
        assert report.verdict == "proceed"

    async def test_pod_pending_returns_proceed(self):
        """Pod stuck in Pending → proceed."""
        pods = _pod_list(
            _make_pod("shop-demo-abc-1", phase="Pending", ready=False)
        )
        report = await check_live_state(
            action_type="restart_pod",
            namespace="default",
            resource_name="shop-demo",
            k8s_core=_k8s_core(pods),
            k8s_apps=MagicMock(),
        )
        assert report.verdict == "proceed"

    async def test_high_restart_count_returns_proceed(self):
        """restart_count >= 3 even without waiting reason → proceed."""
        pods = _pod_list(
            _make_pod("shop-demo-abc-1", restart_count=4, ready=True, phase="Running")
        )
        report = await check_live_state(
            action_type="restart_deployment",
            namespace="default",
            resource_name="shop-demo",
            k8s_core=_k8s_core(pods),
            k8s_apps=MagicMock(),
        )
        assert report.verdict == "proceed"

    async def test_mixed_pods_one_failing_returns_proceed(self):
        """Even one failing pod → proceed (not all healthy)."""
        pods = _pod_list(
            _make_pod("shop-demo-abc-1", phase="Running", ready=True, restart_count=0),
            _make_pod("shop-demo-abc-2", waiting_reason="CrashLoopBackOff", restart_count=3),
        )
        report = await check_live_state(
            action_type="restart_pod",
            namespace="default",
            resource_name="shop-demo",
            k8s_core=_k8s_core(pods),
            k8s_apps=MagicMock(),
        )
        assert report.verdict == "proceed"

    async def test_no_pods_found_returns_proceed(self):
        """No pods found for deployment → proceed (can't confirm health)."""
        report = await check_live_state(
            action_type="restart_pod",
            namespace="default",
            resource_name="nonexistent",
            k8s_core=_k8s_core(_pod_list()),  # empty
            k8s_apps=MagicMock(),
        )
        assert report.verdict == "proceed"

    async def test_k8s_api_error_returns_unknown(self):
        """K8s API error → unknown (fail-open)."""
        core = MagicMock()
        core.list_namespaced_pod.side_effect = Exception("connection refused")
        report = await check_live_state(
            action_type="restart_pod",
            namespace="default",
            resource_name="shop-demo",
            k8s_core=core,
            k8s_apps=MagicMock(),
        )
        assert report.verdict == "unknown"
        assert report.condition_still_active is True  # fail-open


# ── rollback / kubectl_rollout_undo ───────────────────────────────────────────

class TestRollbackCondition:
    async def test_deployment_healthy_returns_self_healed(self):
        """All replicas available → self_healed."""
        dep = _deployment(desired=3, available=3, ready=3)
        report = await check_live_state(
            action_type="kubectl_rollout_undo",
            namespace="default",
            resource_name="shop-demo",
            k8s_core=MagicMock(),
            k8s_apps=_k8s_apps(dep),
        )
        assert report.verdict == "self_healed"
        assert report.evidence.get("all_replicas_available") is True

    async def test_deployment_degraded_returns_proceed(self):
        """availableReplicas < desired → proceed."""
        dep = _deployment(desired=3, available=1, ready=1)
        report = await check_live_state(
            action_type="rollback_deployment",
            namespace="default",
            resource_name="shop-demo",
            k8s_core=MagicMock(),
            k8s_apps=_k8s_apps(dep),
        )
        assert report.verdict == "proceed"
        assert report.evidence["available_replicas"] == 1
        assert report.evidence["desired_replicas"] == 3

    async def test_api_error_returns_unknown(self):
        apps = MagicMock()
        apps.read_namespaced_deployment.side_effect = Exception("timeout")
        report = await check_live_state(
            action_type="kubectl_rollout_undo",
            namespace="default",
            resource_name="shop-demo",
            k8s_core=MagicMock(),
            k8s_apps=apps,
        )
        assert report.verdict == "unknown"


# ── scale_deployment ──────────────────────────────────────────────────────────

class TestScaleCondition:
    async def test_all_replicas_up_returns_self_healed(self):
        dep = _deployment(desired=2, available=2)
        report = await check_live_state(
            action_type="scale_deployment",
            namespace="default",
            resource_name="shop-demo",
            k8s_core=MagicMock(),
            k8s_apps=_k8s_apps(dep),
        )
        assert report.verdict == "self_healed"

    async def test_degraded_deployment_returns_proceed(self):
        dep = _deployment(desired=4, available=2)
        report = await check_live_state(
            action_type="scale_resource",
            namespace="default",
            resource_name="shop-demo",
            k8s_core=MagicMock(),
            k8s_apps=_k8s_apps(dep),
        )
        assert report.verdict == "proceed"


# ── Unknown action type ───────────────────────────────────────────────────────

class TestUnknownActionType:
    async def test_unknown_action_type_proceeds(self):
        report = await check_live_state(
            action_type="custom_action_xyz",
            namespace="default",
            resource_name="shop-demo",
            k8s_core=MagicMock(),
            k8s_apps=MagicMock(),
        )
        assert report.verdict == "proceed"


# ── LiveStateReport fields ────────────────────────────────────────────────────

class TestLiveStateReport:
    async def test_report_has_checked_at(self):
        pods = _pod_list(
            _make_pod("pod-1", phase="Running", ready=True, restart_count=0)
        )
        report = await check_live_state(
            action_type="restart_pod",
            namespace="default",
            resource_name="pod",
            k8s_core=_k8s_core(pods),
            k8s_apps=MagicMock(),
        )
        assert isinstance(report.checked_at, datetime)
        assert report.action_type == "restart_pod"

    async def test_str_repr(self):
        report = LiveStateReport(
            condition_still_active=True,
            verdict="proceed",
            action_type="restart_pod",
        )
        s = str(report)
        assert "restart_pod" in s
        assert "proceed" in s
