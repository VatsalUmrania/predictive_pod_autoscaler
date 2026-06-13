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
        "MetricsAgent", "K8sAgent", "DBAgent", "NginxAgent",
        "NetworkAgent", "ConfigAgent", "GitAgent",
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
