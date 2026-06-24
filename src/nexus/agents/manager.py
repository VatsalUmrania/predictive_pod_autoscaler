# src/nexus/agents/manager.py

from __future__ import annotations

import asyncio
import os

from nexus.agents import (
    BaseAgent,
    ConfigAgent,
    GitAgent,
    K8sAgent,
    MetricsAgent,
    NetworkAgent,
    NginxAgent,
    db_agent_from_env,
)


class AgentManager:
    """Manages the lifecycle of all 7 domain agents.

    The seven agents produced here:
        MetricsAgent, K8sAgent, DBAgent, NginxAgent, NetworkAgent,
        ConfigAgent, GitAgent.

    Each agent runs its sense→publish loop until stop() is called.
    All agents share the same NATSClient (single JetStream connection).
    """

    def __init__(self, nats_client, prometheus_url: str | None = None) -> None:
        self.nats_client = nats_client
        # GitAgent needs an explicit repo path; default to "." (CWD) so the runtime
        # is honest about what it sees rather than silently scanning nothing useful.
        # Override with $NEXUS_GIT_REPO_PATH in production.
        git_repo_path = os.getenv("NEXUS_GIT_REPO_PATH", ".")
        self.agents: list[BaseAgent] = [
            MetricsAgent(nats_client, prometheus_url=prometheus_url),
            K8sAgent(nats_client),
            # DBAgent pulls adapters from NEXUS_POSTGRES_HOST / NEXUS_MYSQL_HOST /
            # NEXUS_MONGODB_URI env vars — db_agent_from_env() handles construction.
            db_agent_from_env(nats_client),
            NginxAgent(nats_client),
            NetworkAgent(nats_client),
            ConfigAgent(nats_client),
            GitAgent(nats_client, repo_path=git_repo_path),
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
