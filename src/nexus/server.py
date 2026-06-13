# src/nexus/server.py
"""NexusServer — owns all NEXUS components and manages their asyncio task lifecycle."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, NoReturn

import uvicorn

if TYPE_CHECKING:
    import uvicorn

from nexus.agents.manager import AgentManager
from nexus.bus.nats_client import NATSClient
from nexus.governance.action_ladder import (
    ActionLadder,
    GovernanceCircuitBreaker,
    HumanApprovalQueue,
)
from nexus.governance.audit_trail import AuditTrail
from nexus.governance.cooldown_store import CooldownStore
from nexus.governance.policy_engine import PolicyEngine
from nexus.governance.rollback_registry import RollbackRegistry
from nexus.governance.runbook_executor import RunbookExecutor
from nexus.integration.notifier import Notifier
from nexus.learning.ppa_outcome_tracker import PpaOutcomeTracker
from nexus.observability.status_api import app as status_api
from nexus.observability.status_api import context
from nexus.predictive.prescaler import Prescaler
from nexus.reasoning.confidence_scorer import ConfidenceScorer
from nexus.reasoning.event_correlator import EventCorrelator
from nexus.reasoning.orchestrator import NexusOrchestrator
from nexus.reasoning.rca_engine import RCAEngine

logger = logging.getLogger(__name__)


class NexusServer:
    """Owns all NEXUS components and manages their asyncio task lifecycle.

    Create via ``await NexusServer.create(...)`` — never instantiate directly.

    On start():
      - AgentManager starts 7 domain agents (sense loops, NATS publishes)
      - PpaOutcomeTracker starts its polling loop (prediction outcome resolution)
      - NexusOrchestrator starts its event-consumption loop
      - uvicorn serves the FastAPI status API on port 8080

    On shutdown (stop()):
      - All tasks are cancelled and gathered
      - AgentManager, Notifier, outcome_tracker shut down gracefully
      - NATS connection is closed
    """

    def __init__(
        self,
        nats_client: NATSClient,
        orchestrator: NexusOrchestrator,
        prescaler: Prescaler,
        outcome_tracker: PpaOutcomeTracker,
        notifier: Notifier,
        agent_manager: AgentManager,
        status_api,  # FastAPI app
    ) -> None:
        self.nats_client = nats_client
        self.orchestrator = orchestrator
        self.prescaler = prescaler
        self.outcome_tracker = outcome_tracker
        self.notifier = notifier
        self.agent_manager = agent_manager
        self.status_api = status_api
        self._tasks: list[asyncio.Task] = []

    # Factory
    @classmethod
    async def create(
        cls,
        nats_url: str = "nats://localhost:4222",
        runbook_dir: str | None = None,
        prometheus_url: str = "http://prometheus:9090",
        gemini_api_key: str | None = None,
        status_port: int = 8080,
    ) -> NexusServer:
        """
        Async factory: connects to NATS, creates and wires all components.

        Does NOT start any background tasks — that happens in start().

        NOTE: outcome_tracker.start() is intentionally NOT called here.
        Calling it here AND in start() would create two polling loops.
        It is started only in start().
        """
        nats = NATSClient(nats_url=nats_url)
        await nats.connect()

        # Prescaler
        prescaler = Prescaler(nats_client=nats)
        await prescaler.subscribe_to_ppa_predictions()

        # ── Outcome tracker
        outcome_tracker = PpaOutcomeTracker(nats_client=nats)
        # NOTE: do NOT call outcome_tracker.start() here; see docstring above.

        # ── Reasoning components ──────────────────────────────────────────────
        if runbook_dir is None:
            import pathlib
            runbook_dir = pathlib.Path(__file__).parent.parent.parent / "deploy" / "nexus" / "runbooks"

        audit_trail = AuditTrail()
        rollback_reg = RollbackRegistry()
        ladder = ActionLadder(
            policy_engine=PolicyEngine(),
            cooldown_store=CooldownStore(),
            approval_queue=HumanApprovalQueue(),
            governance_cb=GovernanceCircuitBreaker(
                failure_threshold=3,
                nats_client=nats,
            ),
        )
        executor = RunbookExecutor(
            nats_client=nats,
            runbook_dir=runbook_dir,
            audit_trail=audit_trail,
            action_ladder=ladder,
            rollback_registry=rollback_reg,
            prometheus_url=prometheus_url,
        )

        rca_engine = RCAEngine(api_key=gemini_api_key)
        confidence_scorer = ConfidenceScorer()
        correlator = EventCorrelator()

        orchestrator = NexusOrchestrator(
            nats_client=nats,
            correlator=correlator,
            rca_engine=rca_engine,
            confidence_scorer=confidence_scorer,
            executor=executor,
        )

        # ── Notifier (sync — manages its own background task internally) ────────
        notifier = Notifier()
        notifier.start_background(nats)  # returns None; _listen loop is internal

        # ── Agent manager ────────────────────────────────────────────────────
        agent_manager = AgentManager(nats_client=nats)

        # ── Pre-populate NexusContext so status_api lifespan skips self-init ───
        context.orchestrator = orchestrator
        context.prescaler = prescaler
        context.ppa_outcome_tracker = outcome_tracker
        context.audit_trail = audit_trail
        context.feedback_loop = None
        context.outcome_store = None
        context.knowledge_base = None
        context.runbook_library = None

        return cls(
            nats_client=nats,
            orchestrator=orchestrator,
            prescaler=prescaler,
            outcome_tracker=outcome_tracker,
            notifier=notifier,
            agent_manager=agent_manager,
            status_api=status_api,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Start all background tasks.

        - agent_manager.start() spawns 7 agent run-loop Tasks and captures their handles
        - outcome_tracker.start() creates the polling Task
        - orchestrator.start() opens NATS subscriptions and begins event processing
        - uvicorn serves the FastAPI status API on port 8080
        """
        await self.agent_manager.start()

        # outcome_tracker.start() must be here, NOT in create(), to avoid a double loop.
        await self.outcome_tracker.start()

        self._tasks = [
            asyncio.create_task(self.orchestrator.start(), name="orchestrator"),
            asyncio.create_task(
                uvicorn.Server(
                    uvicorn.Config(self.status_api, host="0.0.0.0", port=8080)
                ).serve(),
                name="status-api",
            ),
            *self.agent_manager._tasks,                       # agent run-loop handles
        ]

    async def stop(self) -> None:
        """
        Graceful shutdown: cancel all tasks, stop subsystems, close NATS.
        """
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

        self.agent_manager.stop()
        await self.outcome_tracker.stop()
        self.notifier.stop()          # cancels Notifier._nats_task — never skip this
        await self.nats_client.close()

    async def run_forever(self) -> NoReturn:
        """Block until all server tasks complete (never returns normally)."""
        await asyncio.gather(*self._tasks)
