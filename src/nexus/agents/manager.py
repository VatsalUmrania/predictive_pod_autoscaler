# src/nexus/agents/manager.py

from __future__ import annotations

import asyncio

from nexus.agents import (
    BaseAgent,
    ConfigAgent,
    DBAgent,
    GitAgent,
    K8sAgent,
    MetricsAgent,
    NetworkAgent,
    NginxAgent,
)


class AgentManager:
    """Manages the lifecycle of all 7 domain agents.

    Each agent runs its sense→publish loop until stop() is called.
    All agents share the same NATSClient (single JetStream connection).
    """

    def __init__(self, nats_client) -> None:
        self.nats_client = nats_client
        self.agents: list[BaseAgent] = [
            MetricsAgent(nats_client),
            K8sAgent(nats_client),
            DBAgent(nats_client, adapters=[]),
            NginxAgent(nats_client),
            NetworkAgent(nats_client),
            ConfigAgent(nats_client),
            GitAgent(nats_client, repo_path="."),
        ]
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        """Spawn and store handles to all agent run-loop Tasks."""
        self._tasks = [
            asyncio.create_task(agent.run(), name=f"agent-{agent.__class__.__name__}")
            for agent in self.agents
        ]

    async def stop(self) -> None:
        """Signal all agents to stop and await their graceful shutdown."""
        for agent in self.agents:
            agent.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
