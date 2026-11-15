"""
NexusServer Component Wiring and Bootstrap
==========================================
Factory helper to initialize, wire, and register all NEXUS subsystems.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)


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

    agent_manager = srv_mod.AgentManager(
        nats_client=nats, prometheus_url=prometheus_url
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
        "agent_manager": agent_manager,
        "feedback_loop": feedback_loop,
        "status_api": srv_mod.status_api,
        "orchestrator": orchestrator,
    }
