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
        self._running = False
        self._monitor_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Spawn and store handles to all agent run-loop Tasks."""
        self._running = True
        self._tasks = [
            asyncio.create_task(agent.run(), name=f"agent-{agent.__class__.__name__}")
            for agent in self.agents
        ]
        self._monitor_task = asyncio.create_task(
            self._monitor_loop(), name="agent-manager-monitor"
        )

    async def _monitor_loop(self) -> None:
        import logging

        logger = logging.getLogger(__name__)
        while self._running:
            try:
                await asyncio.sleep(5.0)
                for i, agent in enumerate(self.agents):
                    if self._tasks[i].done():
                        logger.error(
                            f"[AgentManager] {agent.__class__.__name__} crashed! Restarting..."
                        )
                        self._tasks[i] = asyncio.create_task(
                            agent.run(), name=f"agent-{agent.__class__.__name__}"
                        )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"[AgentManager] Monitor loop error: {exc}")

    async def stop(self) -> None:
        """Signal all agents to stop and await their graceful shutdown."""
        self._running = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        for agent in self.agents:
            agent.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
