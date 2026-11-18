"""
NexusServer & Component Wiring Bootstrap
========================================
Owns all NEXUS components, manages their asyncio task lifecycle, and wires
subsystems together cleanly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from typing import Any

import uvicorn

from nexus.bus.nats.client import NATSClient
from nexus.db.client import get_database_client
from nexus.governance.approval_queue import HumanApprovalQueue
from nexus.governance.audit_trail import AuditTrail
from nexus.governance.circuit_breaker import GovernanceCircuitBreaker
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


def _parse_horizon(raw: Any) -> int:
    if isinstance(raw, int):
        return raw
    m = re.search(r"(\d+)m$", str(raw))
    return int(m.group(1)) if m else 10


async def bootstrap_components(
    srv_mod: Any,
    nats_url: str = "nats://localhost:4222",
    runbook_dir: str | None = None,
    prometheus_url: str = "http://prometheus:9090",
    gemini_api_key: str | None = None,
    status_port: int = 8080,
) -> dict[str, Any]:
    """Connects to NATS, creates and wires all subsystems using srv_mod for symbol lookups."""
    nats = srv_mod.NATSClient(nats_url=nats_url)
    await nats.connect()

    try:
        from nexus.integration.token_store import get_token_store

        _ts = get_token_store()
        await _ts.init()
    except Exception as _ts_exc:
        logger.warning(f"[NexusServer] TokenStore init failed (non-fatal): {_ts_exc}")

    db_client = await srv_mod.get_database_client()
    logger.info("[NexusServer] PostgreSQL client connected")

    audit_trail = srv_mod.AuditTrail(db_client=db_client)
    await audit_trail.initialize()

    opa_url = os.getenv("NEXUS_OPA_URL", "http://localhost:8181")
    cooldown_store = srv_mod.CooldownStore(db_client=db_client)
    await cooldown_store.connect()

    policy_engine = srv_mod.PolicyEngine(opa_url=opa_url)
    srv_mod.HumanApprovalQueue(nats_client=nats)
    srv_mod.GovernanceCircuitBreaker(
        failure_threshold=3,
        nats_client=nats,
    )

    prescaler = srv_mod.Prescaler(nats_client=nats, action_ladder=None)
    await prescaler.subscribe_to_ppa_predictions()

    outcome_tracker = srv_mod.PpaOutcomeTracker(
        nats_client=nats, prometheus_url=prometheus_url
    )

    async def _outcome_tracker_handler(data: dict, subject: str) -> None:
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
            logger.warning(f"[OutcomeTracker] handler error: {exc}")

    await nats.subscribe_raw(
        "ppa.predictions.>",
        handler=_outcome_tracker_handler,
        stream_name="PPA_PREDICTIONS",
    )

    outcome_store = srv_mod.OutcomeStore(db_client=db_client)
    await outcome_store.connect()
    knowledge_base = srv_mod.KnowledgeBase(db_client=db_client)
    await knowledge_base.initialize()

    confidence_scorer = srv_mod.ConfidenceScorer()

    workflow = srv_mod.IncidentWorkflow(nats_client=nats)
    try:
        orchestrator = srv_mod.NexusOrchestrator(nats_client=nats)
        if orchestrator is not None and orchestrator is not workflow:
            workflow = orchestrator
    except Exception:
        orchestrator = workflow

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

    notifier = srv_mod.Notifier()
    notifier.start_background(nats)

    feedback_loop = await srv_mod.build_feedback_loop(
        confidence_scorer=confidence_scorer,
        nats_client=nats,
        ppa_outcome_tracker=outcome_tracker,
        outcome_store=outcome_store,
        knowledge_base=knowledge_base,
    )

    srv_mod.context.workflow = workflow
    srv_mod.context.orchestrator = workflow
    srv_mod.context.prescaler = prescaler
    srv_mod.context.ppa_outcome_tracker = outcome_tracker
    srv_mod.context.audit_trail = audit_trail
    srv_mod.context.feedback_loop = feedback_loop
    srv_mod.context.outcome_store = outcome_store
    srv_mod.context.knowledge_base = knowledge_base
    srv_mod.context.db_client = db_client
    srv_mod.context.policy_engine = policy_engine

    return {
        "nats_client": nats,
        "workflow": workflow,
        "prescaler": prescaler,
        "outcome_tracker": outcome_tracker,
        "notifier": notifier,
        "feedback_loop": feedback_loop,
        "status_api": srv_mod.status_api,
        "orchestrator": orchestrator,
    }


class NexusServer:
    """Owns all NEXUS components and manages their asyncio task lifecycle."""

    def __init__(
        self,
        nats_client: NATSClient,
        workflow: IncidentWorkflow,
        prescaler: Prescaler,
        outcome_tracker: PpaOutcomeTracker,
        notifier: Notifier,
        feedback_loop: Any,
        status_api: Any,
        orchestrator: Any = None,
    ) -> None:
        self.nats_client = nats_client
        self.workflow = workflow
        self.orchestrator = orchestrator or workflow
        self.prescaler = prescaler
        self.outcome_tracker = outcome_tracker
        self.notifier = notifier
        self.feedback_loop = feedback_loop
        self.status_api = status_api
        self._tasks: list[asyncio.Task[Any]] = []

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
        await self.outcome_tracker.start()

        if self.feedback_loop is not None:
            await self.feedback_loop.start()

        self._tasks = [
            asyncio.create_task(
                self.workflow.start(self.nats_client), name="langgraph-workflow"
            ),
            asyncio.create_task(
                uvicorn.Server(
                    uvicorn.Config(self.status_api, host="0.0.0.0", port=8080)
                ).serve(),
                name="status-api",
            ),
        ]

    async def stop(self) -> None:
        """Graceful shutdown: cancel all tasks, stop subsystems, close NATS."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

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
    "bootstrap_components",
    "NexusServer",
    "NexusOrchestrator",
    "RCAEngine",
    "RunbookExecutor",
    "get_database_client",
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
    "NATSClient",
    "Prescaler",
    "PpaOutcomeTracker",
    "Notifier",
    "IncidentWorkflow",
    "context",
]
