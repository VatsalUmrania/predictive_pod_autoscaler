"""NexusServer — owns all NEXUS components and manages their asyncio task lifecycle."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import TYPE_CHECKING, Any

import uvicorn

if TYPE_CHECKING:
    import uvicorn

from nexus.agents.manager import AgentManager
from nexus.bus.nats_client import NATSClient
from nexus.db.client import get_database_client
from nexus.governance.action_ladder import (
    ActionLadder,
    GovernanceCircuitBreaker,
    HumanApprovalQueue,
)
from nexus.governance.audit_trail import AuditTrail
from nexus.governance.cooldown_store import CooldownStore
from nexus.governance.policy_engine import PolicyEngine
from nexus.governance.rollback_registry import RollbackRegistry
from nexus.graph.workflow import IncidentWorkflow
from nexus.integration.notifier import Notifier
from nexus.learning.feedback_loop import build_feedback_loop
from nexus.learning.knowledge_base import KnowledgeBase
from nexus.learning.outcome_store import OutcomeStore
from nexus.learning.ppa_outcome_tracker import PpaOutcomeTracker
from nexus.observability.status_api import app as status_api
from nexus.observability.status_api import context
from nexus.predictive.prescaler import Prescaler
from nexus.reasoning.confidence_scorer import ConfidenceScorer
from nexus.server_bootstrap import bootstrap_components

logger = logging.getLogger(__name__)


class RunbookExecutor:
    """Backwards-compatibility shim for server tests."""
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass


class RCAEngine:
    """Backwards-compatibility shim for server tests."""
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass


NexusOrchestrator = IncidentWorkflow


class NexusServer:
    """Owns all NEXUS components and manages their asyncio task lifecycle."""

    def __init__(
        self,
        nats_client: NATSClient,
        workflow: IncidentWorkflow,
        prescaler: Prescaler,
        outcome_tracker: PpaOutcomeTracker,
        notifier: Notifier,
        agent_manager: AgentManager,
        feedback_loop,  # FeedbackLoop | None
        status_api,  # FastAPI app
        orchestrator: Any = None,
    ) -> None:
        self.nats_client = nats_client
        self.workflow = workflow
        self.orchestrator = orchestrator or workflow
        self.prescaler = prescaler
        self.outcome_tracker = outcome_tracker
        self.notifier = notifier
        self.agent_manager = agent_manager
        self.feedback_loop = feedback_loop
        self.status_api = status_api
        self._tasks: list[asyncio.Task] = []

    @classmethod
    async def create(
        cls,
        nats_url: str = "nats://localhost:4222",
        runbook_dir: str | None = None,
        prometheus_url: str = "http://prometheus:9090",
        gemini_api_key: str | None = None,
        status_port: int = 8080,
    ) -> NexusServer:
        """Async factory: connects to NATS, creates and wires all components."""
        srv_mod = sys.modules[__name__]
        components = await bootstrap_components(
            srv_mod=srv_mod,
            nats_url=nats_url,
            runbook_dir=runbook_dir,
            prometheus_url=prometheus_url,
            gemini_api_key=gemini_api_key,
            status_port=status_port,
        )
        return cls(**components)

    async def start(self) -> None:
        """Start all background tasks."""
        await self.agent_manager.start()
        await self.outcome_tracker.start()

        if self.feedback_loop is not None:
            await self.feedback_loop.start()

        self._tasks = [
            asyncio.create_task(self.workflow.start(self.nats_client), name="langgraph-workflow"),
            asyncio.create_task(
                uvicorn.Server(
                    uvicorn.Config(self.status_api, host="0.0.0.0", port=8080)
                ).serve(),
                name="status-api",
            ),
        ]
        if self.agent_manager._monitor_task:
            self._tasks.append(self.agent_manager._monitor_task)

    async def stop(self) -> None:
        """Graceful shutdown: cancel all tasks, stop subsystems, close NATS."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

        await self.agent_manager.stop()
        await self.outcome_tracker.stop()
        await self.workflow.stop()
        if self.feedback_loop is not None:
            await self.feedback_loop.stop()
        self.notifier.stop()
        await self.nats_client.close()
        if context.db_client is not None:
            await context.db_client.close()
            context.db_client = None

    async def run_forever(self) -> None:
        """Block until all server tasks complete (never returns normally)."""
        await asyncio.gather(*self._tasks)


__all__ = [
    "NexusServer",
    "NexusOrchestrator",
    "RCAEngine",
    "RunbookExecutor",
    "get_database_client",
    "ActionLadder",
    "GovernanceCircuitBreaker",
    "HumanApprovalQueue",
    "AuditTrail",
    "CooldownStore",
    "PolicyEngine",
    "RollbackRegistry",
    "build_feedback_loop",
    "KnowledgeBase",
    "OutcomeStore",
    "status_api",
    "ConfidenceScorer",
]

