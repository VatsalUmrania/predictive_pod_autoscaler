"""
NEXUS Feedback Loop
====================
Closes the `act → verify → learn` half of the sense→reason→act→verify→learn cycle.

The FeedbackLoop is a background task that runs every N minutes and:
    1. Polls the AuditTrail (via OutcomeStore) for completed healing outcomes
    2. Computes per-runbook success rates → confidence adjustments
    3. Persists adjustments in the KnowledgeBase
    4. Pushes updated adjustment map into the ConfidenceScorer (in-memory)
    5. Runs the RunbookAdvisor → logs and publishes recommendations to NATS
    6. Records signal-type patterns + outcomes in KnowledgeBase for RCA enrichment

Integration with ConfidenceScorer (Phase 4 update):
    The FeedbackLoop calls ConfidenceScorer.set_historical_boosts(boosts)
    where boosts = {runbook_id: delta} from the KnowledgeBase.
    The scorer adds this delta to the blended score AFTER all other factors,
    ensuring learned history influences but does not override real-time signals.

Publication:
    On each cycle, publishes a LEARNING_CYCLE_COMPLETE event to NATS with the
    system KPIs and a summary of recommendations. Used by Phase 7 dashboard.

Configuration:
    NEXUS_FEEDBACK_INTERVAL_S   Poll interval in seconds (default: 300 = 5 min)
    NEXUS_FEEDBACK_WINDOW_DAYS  Lookback window for stats (default: 30 days)
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from nexus.bus.incident_event import AgentType, IncidentEvent, Severity, SignalType
from nexus.bus.nats_client import NATSClient
from nexus.learning.knowledge_base import KnowledgeBase
from nexus.learning.outcome_store import OutcomeStore, RunbookStats, SystemKPIs
from nexus.learning.runbook_advisor import RunbookAdvisor, RunbookRecommendation
from nexus.reasoning.confidence_scorer import ConfidenceScorer

logger = logging.getLogger(__name__)


class FeedbackLoop:
    """
    Background learning coordinator for the NEXUS system.

    Args:
        outcome_store:      OutcomeStore wrapping the AuditTrail DB.
        knowledge_base:     KnowledgeBase for persistence.
        confidence_scorer:  Phase 4 ConfidenceScorer (receives boost updates).
        nats_client:        NATSClient for publishing learning events.
        interval_s:         Polling interval in seconds (default: 300).
        window_days:        Lookback window for stats (default: 30).
    """

    def __init__(
        self,
        outcome_store:     OutcomeStore,
        knowledge_base:    KnowledgeBase,
        confidence_scorer: ConfidenceScorer,
        nats_client:       Optional[NATSClient] = None,
        interval_s:        float = 300.0,
        window_days:       int   = 30,
    ):
        import os
        self._store   = outcome_store
        self._kb      = knowledge_base
        self._scorer  = confidence_scorer
        self._nats    = nats_client
        self._interval     = float(os.getenv("NEXUS_FEEDBACK_INTERVAL_S", str(interval_s)))
        self._window_days  = int(os.getenv("NEXUS_FEEDBACK_WINDOW_DAYS", str(window_days)))
        self._advisor      = RunbookAdvisor(outcome_store=outcome_store)

        # Background task handle
        self._task: Optional[asyncio.Task] = None

        # Observability
        self._cycles_run      = 0
        self._last_cycle_at:  Optional[float] = None
        self._last_kpis:      Optional[Dict[str, Any]] = None
        self._last_recs:      List[Dict[str, Any]] = []
        self._start_time:     Optional[float] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background polling loop (non-blocking)."""
        self._start_time = time.monotonic()
        self._task = asyncio.create_task(self._run(), name="nexus-feedback-loop")
        logger.info(
            f"[FeedbackLoop] Started — "
            f"interval={self._interval}s "
            f"window={self._window_days}d"
        )

    async def stop(self) -> None:
        """Graceful shutdown."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info(
            f"[FeedbackLoop] Stopped — "
            f"cycles_run={self._cycles_run}"
        )

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        """Background polling loop."""
        # Run one cycle immediately on startup to hydrate scorer
        try:
            await self._update_cycle()
        except Exception as exc:
            logger.error(f"[FeedbackLoop] Startup cycle error: {exc}", exc_info=True)

        while True:
            try:
                await asyncio.sleep(self._interval)
                await self._update_cycle()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"[FeedbackLoop] Cycle error: {exc}", exc_info=True)

    # ── One learning cycle ────────────────────────────────────────────────────

    async def _update_cycle(self) -> None:
        """
        One full learning cycle:
            1. Query all runbook stats from AuditTrail
            2. Compute + persist KnowledgeBase adjustments
            3. Push adjustment map to ConfidenceScorer
            4. Run RunbookAdvisor
            5. Publish NATS event
            6. Update signal-pattern records
        """
        cycle_start = time.monotonic()
        self._cycles_run += 1
        logger.info(
            f"[FeedbackLoop] Cycle #{self._cycles_run} — "
            f"querying last {self._window_days} days"
        )

        # ── 1. Query AuditTrail ───────────────────────────────────────────────
        all_stats:  Dict[str, RunbookStats] = await self._store.get_all_runbook_stats(
            days=self._window_days
        )
        system_kpis: SystemKPIs = await self._store.get_system_kpis(
            days=self._window_days
        )

        if not all_stats:
            logger.info("[FeedbackLoop] No healing records in window — skipping adjustments")
        else:
            logger.info(
                f"[FeedbackLoop] Found stats for {len(all_stats)} runbook(s) — "
                f"total_actions={system_kpis.total_actions} "
                f"success_rate={system_kpis.autonomous_success_rate:.0%}"
            )

        # ── 2. Persist KnowledgeBase adjustments ──────────────────────────────
        adjustments: Dict[str, float] = {}
        if all_stats:
            adjustments = await self._kb.bulk_update(all_stats)

        # ── 3. Push boosts to ConfidenceScorer ───────────────────────────────
        all_adjustments = await self._kb.get_all_adjustments()
        self._scorer.set_historical_boosts(all_adjustments)

        # ── 4. Run RunbookAdvisor ─────────────────────────────────────────────
        recs: List[RunbookRecommendation] = self._advisor.analyze(all_stats, system_kpis)

        # Also check chronic targets (async)
        chronic = await self._advisor.find_chronic_targets()
        recs.extend(chronic)

        # ── 5. Update signal-pattern records ─────────────────────────────────
        await self._update_signal_patterns()

        # ── 6. Publish NATS event ─────────────────────────────────────────────
        cycle_ms = int((time.monotonic() - cycle_start) * 1000)
        self._last_kpis    = system_kpis.to_dict()
        self._last_recs    = [r.to_dict() for r in recs]
        self._last_cycle_at = time.monotonic()

        await self._publish_summary(system_kpis, recs, adjustments, cycle_ms)

        logger.info(
            f"[FeedbackLoop] Cycle #{self._cycles_run} complete in {cycle_ms}ms — "
            f"adjustments={len(adjustments)} "
            f"recommendations={len(recs)}"
        )

    async def _update_signal_patterns(self) -> None:
        """
        Read recent outcomes and record signal_type patterns in the KB.
        Maps: incident signal_type combination → runbook_id + outcome.
        """
        recent = await self._store.get_recent_outcomes(limit=50)
        for record in recent:
            if not record.is_completed:
                continue
            # We only have the runbook_id here; signal types come from the
            # incident event. In Phase 7 we'll store correlation_id links.
            # For now, derive a synthetic single-signal pattern from runbook name.
            inferred_signals = _infer_signals_from_runbook(record.runbook_id)
            if inferred_signals:
                await self._kb.record_pattern(
                    signal_types = inferred_signals,
                    runbook_id   = record.runbook_id,
                    success      = record.is_success,
                )

    async def _publish_summary(
        self,
        kpis:         SystemKPIs,
        recs:         List[RunbookRecommendation],
        adjustments:  Dict[str, float],
        cycle_ms:     int,
    ) -> None:
        """Publish a LEARNING_CYCLE_COMPLETE event to NATS."""
        if not self._nats:
            return
        try:
            evt = IncidentEvent(
                agent       = AgentType.ORCHESTRATOR,
                signal_type = SignalType.ANOMALY_DETECTED,   # Closest available type
                severity    = Severity.INFO,
                context     = {
                    "type":           "learning_cycle_complete",
                    "cycle_number":   self._cycles_run,
                    "cycle_ms":       cycle_ms,
                    "system_kpis":    kpis.to_dict(),
                    "adjustments":    {k: round(v, 4) for k, v in adjustments.items()},
                    "recommendations": [r.to_dict() for r in recs[:10]],
                    "timestamp":      datetime.now(timezone.utc).isoformat(),
                },
            )
            await self._nats.publish(evt)
        except Exception as exc:
            logger.debug(f"[FeedbackLoop] NATS publish failed: {exc}")

    # ── Manual trigger ────────────────────────────────────────────────────────

    async def run_now(self) -> Dict[str, Any]:
        """
        Trigger an immediate learning cycle (useful for testing or CLI invocation).
        Returns the cycle summary.
        """
        await self._update_cycle()
        return {
            "cycles_run": self._cycles_run,
            "last_kpis":  self._last_kpis,
            "last_recs":  self._last_recs,
        }

    # ── Observability ─────────────────────────────────────────────────────────

    @property
    def status(self) -> Dict[str, Any]:
        uptime = time.monotonic() - self._start_time if self._start_time else 0.0
        return {
            "cycles_run":       self._cycles_run,
            "uptime_seconds":   round(uptime, 1),
            "interval_s":       self._interval,
            "window_days":      self._window_days,
            "last_kpis":        self._last_kpis,
            "last_recommendations": len(self._last_recs),
        }


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _infer_signals_from_runbook(runbook_id: str) -> Optional[set]:
    """
    Infer the likely triggering signal types from a runbook ID.
    Used for Phase 6 pattern recording until Phase 7 links correlation_ids.
    """
    mapping = {
        "runbook_pod_crashloop_v1":                {"pod_crashloop"},
        "runbook_high_error_rate_post_deploy_v1":  {"high_error_rate", "deploy_event"},
        "runbook_missing_env_key_v1":              {"env_contract_violation"},
        "runbook_dns_resolution_failure_v1":       {"dns_resolution_failure"},
        "runbook_db_connection_exhaustion_v1":     {"db_connection_exhaustion"},
    }
    return mapping.get(runbook_id)


# ──────────────────────────────────────────────────────────────────────────────
# Factory
# ──────────────────────────────────────────────────────────────────────────────

async def build_feedback_loop(
    confidence_scorer: ConfidenceScorer,
    nats_client:       Optional[NATSClient] = None,
    audit_db_path:     Optional[str] = None,
    knowledge_db_path: Optional[str] = None,
    interval_s:        float = 300.0,
) -> FeedbackLoop:
    """
    Build and initialize a FeedbackLoop with its dependencies.

    Args:
        confidence_scorer: Phase 4 ConfidenceScorer (will receive historical boosts).
        nats_client:       Optional NATSClient for publishing learning updates.
        audit_db_path:     Override for AuditTrail DB path.
        knowledge_db_path: Override for KnowledgeBase DB path.
        interval_s:        Polling interval (default 300s / 5 min).

    Returns:
        Initialized FeedbackLoop (not yet started — call loop.start()).
    """
    store = OutcomeStore(db_path=audit_db_path)
    await store.connect()

    kb = KnowledgeBase(db_path=knowledge_db_path)
    await kb.initialize()

    return FeedbackLoop(
        outcome_store     = store,
        knowledge_base    = kb,
        confidence_scorer = confidence_scorer,
        nats_client       = nats_client,
        interval_s        = interval_s,
    )
