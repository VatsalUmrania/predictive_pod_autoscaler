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
from nexus.governance.runbook import RunbookLibrary
from nexus.governance.runbook_executor import RunbookExecutor
from nexus.integration.notifier import Notifier
from nexus.learning.feedback_loop import build_feedback_loop
from nexus.learning.knowledge_base import KnowledgeBase
from nexus.learning.outcome_store import OutcomeStore
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
        feedback_loop,  # FeedbackLoop | None
        status_api,  # FastAPI app
    ) -> None:
        self.nats_client = nats_client
        self.orchestrator = orchestrator
        self.prescaler = prescaler
        self.outcome_tracker = outcome_tracker
        self.notifier = notifier
        self.agent_manager = agent_manager
        self.feedback_loop = feedback_loop
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

        # ── TokenStore — must be initialised before any /apps or /sdk request ────
        # When NexusServer owns the lifecycle, status_api's lifespan skips its
        # own self-init block (to avoid a second NATS connection), so the table
        # would never be created unless we do it here.
        try:
            from nexus.integration.token_store import get_token_store

            _ts = get_token_store()
            await _ts.init()
        except Exception as _ts_exc:
            logger.warning(
                f"[NexusServer] TokenStore init failed (non-fatal): {_ts_exc}"
            )

        # ── Reasoning components ──────────────────────────────────────────────
        if runbook_dir is None:
            import pathlib

            runbook_dir = (
                pathlib.Path(__file__).parent.parent.parent
                / "deploy"
                / "nexus"
                / "runbooks"
            )

        # ── AuditTrail — must be initialized so /audit/* endpoints work ──────
        import os as _os

        audit_db_path = _os.getenv("NEXUS_AUDIT_DB_PATH", "/data/nexus_audit.db")
        knowledge_db_path = _os.getenv(
            "NEXUS_KNOWLEDGE_DB_PATH", "/data/nexus_knowledge.db"
        )
        audit_trail = AuditTrail(db_path=audit_db_path)
        await audit_trail.initialize()

        rollback_reg = RollbackRegistry()

        # Construct RunbookLibrary ONCE so the executor and the status API share one instance
        runbook_library = RunbookLibrary(runbook_dir)

        opa_url = _os.getenv("NEXUS_OPA_URL", "http://localhost:8181")
        redis_url = _os.getenv("NEXUS_REDIS_URL")

        ladder = ActionLadder(
            policy_engine=PolicyEngine(opa_url=opa_url),
            cooldown_store=CooldownStore(redis_url=redis_url),
            approval_queue=HumanApprovalQueue(),
            governance_cb=GovernanceCircuitBreaker(
                failure_threshold=3,
                nats_client=nats,
            ),
        )

        # Prescaler — built AFTER the action_ladder so AUTONOMOUS mode can call into it
        prescaler = Prescaler(nats_client=nats, action_ladder=ladder)
        await prescaler.subscribe_to_ppa_predictions()

        # ── Outcome tracker — subscribe to ppa.predictions.* to close the loop
        outcome_tracker = PpaOutcomeTracker(
            nats_client=nats, prometheus_url=prometheus_url
        )

        async def _outcome_tracker_handler(data: dict, subject: str) -> None:
            """Route raw ppa.predictions.* events into the OutcomeTracker."""
            import re

            def _parse_horizon(raw) -> int:
                if isinstance(raw, int):
                    return raw
                m = re.search(r"(\d+)m$", str(raw))
                return int(m.group(1)) if m else 10

            try:
                parts = subject.split(".")
                deployment = parts[-1] if len(parts) > 2 else "unknown"
                await outcome_tracker.on_prediction(
                    deployment=data.get("deployment", deployment),
                    namespace=data.get("namespace", "default"),
                    predicted_rps=float(data.get("predicted_rps", 0)),
                    current_rps=float(data.get("current_rps", 0)),
                    confidence=float(data.get("confidence", 0)),
                    horizon_minutes=_parse_horizon(data.get("horizon_minutes", 10)),
                    model_version=data.get("model_version", "unknown"),
                    raw_features=data.get("raw_features", {}),
                    event_timestamp=data.get("timestamp", ""),
                )
            except Exception as exc:
                import logging as _log

                _log.getLogger(__name__).warning(
                    f"[OutcomeTracker] handler error: {exc}"
                )

        await nats.subscribe_raw(
            "ppa.predictions.>",
            handler=_outcome_tracker_handler,
            # No durable_name: ephemeral consumer per-pod.
            # Durable push consumers are exclusive (one active subscriber) —
            # during a rolling deploy the new pod collides with the old pod's consumer.
            stream_name="PPA_PREDICTIONS",
        )
        # NOTE: do NOT call outcome_tracker.start() here; see docstring above.

        executor = RunbookExecutor(
            nats_client=nats,
            audit_trail=audit_trail,
            action_ladder=ladder,
            rollback_registry=rollback_reg,
            library=runbook_library,
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
        # Hydrate policy cache so Notifier._get_webhook() can resolve Slack URLs.
        # Without this refresh, _policy_cache is empty until an HTTP request hits the
        # dashboard, so Slack notifications stay silent.
        try:
            from nexus.integration.dashboard import refresh_policy_cache

            _loaded = refresh_policy_cache()
            logger.info(
                f"[NexusServer] Pre-cached {_loaded} selfheal.yaml policies for Notifier"
            )
        except Exception as _pc_exc:
            logger.warning(
                f"[NexusServer] Policy cache refresh failed (non-fatal): {_pc_exc}"
            )

        notifier = Notifier()
        notifier.start_background(nats)  # returns None; _listen loop is internal

        # ── Learning plane: OutcomeStore + KnowledgeBase + FeedbackLoop ──────
        # Open OutcomeStore + KnowledgeBase ONCE and share between FeedbackLoop and
        # NexusContext — earlier opening them twice led to two SQLite connections
        # racing on the same database file.
        outcome_store = OutcomeStore(db_path=audit_db_path)
        await outcome_store.connect()
        knowledge_base = KnowledgeBase(db_path=knowledge_db_path)
        await knowledge_base.initialize()

        feedback_loop = await build_feedback_loop(
            confidence_scorer=confidence_scorer,
            nats_client=nats,
            ppa_outcome_tracker=outcome_tracker,
            outcome_store=outcome_store,
            knowledge_base=knowledge_base,
        )

        # ── Agent manager ────────────────────────────────────────────────────
        agent_manager = AgentManager(nats_client=nats, prometheus_url=prometheus_url)

        # ── Pre-populate NexusContext so status_api lifespan skips self-init ───
        context.orchestrator = orchestrator
        context.prescaler = prescaler
        context.ppa_outcome_tracker = outcome_tracker
        context.audit_trail = audit_trail
        context.feedback_loop = feedback_loop
        context.outcome_store = outcome_store
        context.knowledge_base = knowledge_base
        context.runbook_library = runbook_library

        return cls(
            nats_client=nats,
            orchestrator=orchestrator,
            prescaler=prescaler,
            outcome_tracker=outcome_tracker,
            notifier=notifier,
            agent_manager=agent_manager,
            feedback_loop=feedback_loop,
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

        # Start feedback loop (learning plane)
        if self.feedback_loop is not None:
            await self.feedback_loop.start()

        self._tasks = [
            asyncio.create_task(self.orchestrator.start(), name="orchestrator"),
            asyncio.create_task(
                uvicorn.Server(
                    uvicorn.Config(self.status_api, host="0.0.0.0", port=8080)
                ).serve(),
                name="status-api",
            ),
            *self.agent_manager._tasks,  # agent run-loop handles
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
        if self.feedback_loop is not None:
            await self.feedback_loop.stop()
        self.notifier.stop()  # cancels Notifier._nats_task — never skip this
        await self.nats_client.close()

    async def run_forever(self) -> NoReturn:
        """Block until all server tasks complete (never returns normally)."""
        await asyncio.gather(*self._tasks)
