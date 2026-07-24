import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.agents.manager import AgentManager


def test_agent_manager_instantiates_all_7_agents():
    mock_nats = MagicMock()
    manager = AgentManager(nats_client=mock_nats)

    assert len(manager.agents) == 7
    agent_types = {type(a).__name__ for a in manager.agents}
    expected = {
        "MetricsAgent",
        "K8sAgent",
        "DBAgent",
        "NginxAgent",
        "NetworkAgent",
        "ConfigAgent",
        "GitAgent",
    }
    assert agent_types == expected


@pytest.mark.asyncio
async def test_start_spawns_agent_run_tasks():
    mock_nats = MagicMock()
    manager = AgentManager(nats_client=mock_nats)

    for agent in manager.agents:
        agent.run = AsyncMock(return_value=None)

    await manager.start()

    assert len(manager._tasks) == 7
    assert all(isinstance(t, asyncio.Task) for t in manager._tasks)


@pytest.mark.asyncio
async def test_stop_cancels_all_agent_run_tasks():
    mock_nats = MagicMock()
    manager = AgentManager(nats_client=mock_nats)

    async def slow_run():
        await asyncio.sleep(3600)

    for agent in manager.agents:
        agent.run = slow_run

    await manager.start()

    await manager.stop()

    assert manager._tasks == []


# ── Meta-self-healing: the _monitor_loop restarts crashed agents ───────────────


# The real asyncio.sleep, captured at import time (before any monkeypatch).
# Tests use _REAL_SLEEP(0) to genuinely yield control to the scheduler so agent
# tasks can be started/crashed/resumed between monitor polls — independent of
# whatever we patch asyncio.sleep with inside the monitor.
_REAL_SLEEP = asyncio.sleep


def _make_gated_sleep(gate: asyncio.Event):
    """A replacement for asyncio.sleep that blocks until ``gate`` is set, then
    clears it. Lets a test deterministically advance the monitor one iteration
    at a time (set gate → monitor's sleep returns → one poll → re-blocks),
    instead of racing the 5s real timer.
    """

    async def _sleep(_delay: float, *_a: object, **_kw: object) -> None:
        await gate.wait()
        gate.clear()  # consume exactly one advance per monitor iteration

    return _sleep


@pytest.mark.asyncio
async def test_start_spawns_monitor_task():
    """start() must also create the meta-self-healing monitor task."""
    manager = AgentManager(nats_client=MagicMock())
    for agent in manager.agents:
        agent.run = AsyncMock()

    await manager.start()
    try:
        assert manager._monitor_task is not None
        assert isinstance(manager._monitor_task, asyncio.Task)
        assert not manager._monitor_task.done()
    finally:
        await manager.stop()
        try:
            await manager._monitor_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_monitor_loop_restarts_a_crashed_agent(monkeypatch):
    """A crashed agent (run() raises) must be detected and restarted by the
    monitor on its next poll."""
    gate = asyncio.Event()
    monkeypatch.setattr(asyncio, "sleep", _make_gated_sleep(gate))

    manager = AgentManager(nats_client=MagicMock())
    invocations = {a.__class__.__name__: 0 for a in manager.agents}
    blockers = {a.__class__.__name__: asyncio.Event() for a in manager.agents}

    def make_run(name: str):
        async def _run():
            invocations[name] += 1
            if invocations[name] == 1:
                raise RuntimeError(f"{name} crashed")
            await blockers[name].wait()  # restarted instance parks forever

        return _run

    for agent in manager.agents:
        agent.run = make_run(agent.__class__.__name__)

    await manager.start()
    try:
        # 1) Let the scheduler start every agent task + the monitor. Each agent
        #    crashes synchronously (raise), so one cooperative yield drains all.
        await _REAL_SLEEP(0)
        assert set(invocations.values()) == {1}, invocations
        # Monitor parked at first gated sleep, agent tasks are done (crashed).
        assert all(task.done() for task in manager._tasks)

        # 2) Advance the monitor exactly one iteration → it sees every task
        #    done and schedules a replacement for each.
        gate.set()
        # Drain: monitor resumes, restarts all 7 (call_soon), re-parks at the
        # next gated sleep; the restarted tasks also run in this batch and park
        # on their blocker.
        for _ in range(3):
            await _REAL_SLEEP(0)

        assert invocations == {name: 2 for name in invocations}, invocations
        assert manager._monitor_task is not None
        assert not manager._monitor_task.done()
        # Restarted agents are now parked on blockers, not done.
        assert all(not task.done() for task in manager._tasks)
    finally:
        await manager.stop()

    # stop() cancels + clears every agent task.
    assert manager._tasks == []
    try:
        await manager._monitor_task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_monitor_loop_does_not_restart_running_agent(monkeypatch):
    """A healthy (still-armed/parked) agent must NOT be restarted by the monitor."""
    gate = asyncio.Event()
    monkeypatch.setattr(asyncio, "sleep", _make_gated_sleep(gate))

    manager = AgentManager(nats_client=MagicMock())
    blockers = {a.__class__.__name__: asyncio.Event() for a in manager.agents}
    invocations = {a.__class__.__name__: 0 for a in manager.agents}

    def make_run(name: str):
        async def _run():
            invocations[name] += 1
            await blockers[name].wait()  # never complete → never "done"

        return _run

    for agent in manager.agents:
        agent.run = make_run(agent.__class__.__name__)

    await manager.start()
    try:
        # Start everything; each agent increments to 1 then parks on its blocker.
        await _REAL_SLEEP(0)
        assert invocations == {name: 1 for name in invocations}, invocations
        assert all(not task.done() for task in manager._tasks)

        # Advance several monitor iterations — nothing is done, so no restarts.
        for _ in range(5):
            gate.set()
            await _REAL_SLEEP(0)

        assert invocations == {name: 1 for name in invocations}, invocations
        assert all(not task.done() for task in manager._tasks)
    finally:
        await manager.stop()

    assert manager._tasks == []
    try:
        await manager._monitor_task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_stop_cancels_monitor_task(monkeypatch):
    """stop() must cancel the monitor so it doesn't outlive the manager."""
    gate = asyncio.Event()
    monkeypatch.setattr(asyncio, "sleep", _make_gated_sleep(gate))

    manager = AgentManager(nats_client=MagicMock())
    for agent in manager.agents:
        blocker = asyncio.Event()

        async def _block(_b=blocker):
            await _b.wait()

        agent.run = _block

    await manager.start()
    monitor = manager._monitor_task
    assert monitor is not None and not monitor.done()

    await manager.stop()

    assert monitor.done()
    assert manager._tasks == []
    try:
        await monitor
    except asyncio.CancelledError:
        pass
