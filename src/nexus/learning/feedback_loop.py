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
    7. (P3b) Tracks per-incident resolution: whether an action's cluster_id stops
       emitting signals within a verification window after the action — this
       provides a direct "did THIS incident actually resolve?" signal that
       supplements the hourly aggregate boost.

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
    NEXUS_INCIDENT_VERIFY_WINDOW_S  Time to wait after an action to confirm
                                    the incident is resolved (default: 60s)
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from nexus.bus.incident_event import AgentType, IncidentEvent, Severity, SignalType
from nexus.bus.nats_client import NATSClient
from nexus.learning.knowledge_base import KnowledgeBase
from nexus.learning.outcome_store import OutcomeStore, RunbookStats, SystemKPIs
from nexus.learning.ppa_outcome_tracker import PpaOutcomeTracker
from nexus.learning.runbook_advisor import RunbookAdvisor, RunbookRecommendation
from nexus.reasoning.confidence_scorer import ConfidenceScorer

logger = logging.getLogger(__name__)


class PendingIncidentVerification:
    """
    Tracks an action's incident_id awaiting resolution verification.
    If the incident doesn't re-appear within the verify window, it's marked resolved.
    """

    __slots__ = ("incident_id", "runbook_id", "action_type", "outcome", "action_time")

    def __init__(
        self,
        incident_id: str,
        runbook_id: str,
        action_type: str,
        outcome: str,
    ) -> None:
        self.incident_id = incident_id
        self.runbook_id = runbook_id
        self.action_type = action_type
        self.outcome = outcome
        self.action_time = time.monotonic()

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "runbook_id": self.runbook_id,
            "action_type": self.action_type,
            "outcome": self.outcome,
            "action_time": self.action_time,
        }


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
        verify_window_s:    Seconds to wait after an action before checking
                            if the incident is resolved (default: 60).
    """

    def __init__(
        self,
        outcome_store: OutcomeStore,
        knowledge_base: KnowledgeBase,
        confidence_scorer: ConfidenceScorer,
        nats_client: NATSClient | None = None,
        ppa_outcome_tracker: PpaOutcomeTracker | None = None,
        interval_s: float = 300.0,
        window_days: int = 30,
        verify_window_s: float = 60.0,
    ):
        import os

        self._store = outcome_store
        self._kb = knowledge_base
        self._scorer = confidence_scorer
        self._nats = nats_client
        self._ppa_tracker = ppa_outcome_tracker
        self._interval = float(os.getenv("NEXUS_FEEDBACK_INTERVAL_S", str(interval_s)))
        self._window_days = int(
            os.getenv("NEXUS_FEEDBACK_WINDOW_DAYS", str(window_days))
        )
        self._verify_window_s = float(
            os.getenv("NEXUS_INCIDENT_VERIFY_WINDOW_S", str(verify_window_s))
        )
        self._advisor = RunbookAdvisor(outcome_store=outcome_store)

        # Tracking incident verification (P3b)
        self._pending_verifications: dict[str, PendingIncidentVerification] = {}
        # incident_ids that have recently fired — used to detect re-appearance
        self._recent_incidents: dict[str, float] = {}

        # Background task handle
        self._task: asyncio.Task | None = None

        # Observability
        self._cycles_run = 0
        self._last_cycle_at: float | None = None
        self._last_kpis: dict[str, Any] | None = None
        self._last_recs: list[dict[str, Any]] = []
        self._start_time: float | None = None

    # Lifecycle

    async def start(self) -> None:
        """Start the background polling loop (non-blocking)."""
        self._start_time = time.monotonic()
        self._task = asyncio.create_task(self._run(), name="nexus-feedback-loop")
        # Subscribe to action events to record real signal_type → runbook patterns
        if self._nats is not None:
            try:
                await self._nats.subscribe_raw(
                    subject_pattern="nexus.actions.>",
                    handler=self._on_action_event,
                )
                logger.info(
                    "[FeedbackLoop] Subscribed to nexus.actions.> for pattern recording"
                )
            except Exception as exc:
                logger.warning(
                    f"[FeedbackLoop] Pattern subscription failed (non-fatal): {exc}"
                )

            # P3b: also subscribe to incident stream to track re-appearance
            try:
                await self._nats.subscribe_raw(
                    subject_pattern="nexus.incidents.>",
                    handler=self._on_incident_event,
                )
                logger.info(
                    "[FeedbackLoop] Subscribed to nexus.incidents.> for resolution tracking"
                )
            except Exception as exc:
                logger.warning(
                    f"[FeedbackLoop] Incident subscription failed (non-fatal): {exc}"
                )

        logger.info(
            f"[FeedbackLoop] Started — "
            f"interval={self._interval}s "
            f"window={self._window_days}d "
            f"verify_window={self._verify_window_s}s"
        )

    async def _on_action_event(self, data: dict, subject: str) -> None:
        """Record a real signal_type → runbook_id pattern from a healing action.

        P3b: also register the incident_id for resolution verification.
        """
        try:
            signal_type = data.get("signal_type")
            runbook_id = data.get("runbook_id")
            outcome = data.get("outcome", "")
            incident_id = data.get("incident_id")
            action_type = data.get("action_type", "unknown")

            if not signal_type or not runbook_id:
                return

            success = outcome in ("success", "rolled_back")
            await self._kb.record_pattern(
                signal_types={signal_type},
                runbook_id=runbook_id,
                success=success,
            )

            # P3b: if action completed (not stuck in approval), schedule verification
            if incident_id and outcome in ("success", "rolled_back"):
                # Only verify actions that actually touched the cluster
                # (governance_blocked / pending_approval don't execute)
                self._pending_verifications[incident_id] = PendingIncidentVerification(
                    incident_id=incident_id,
                    runbook_id=runbook_id,
                    action_type=action_type,
                    outcome=outcome,
                )
                logger.debug(
                    f"[FeedbackLoop] Scheduled verification for incident {incident_id} "
                    f"(runbook={runbook_id}, outcome={outcome})"
                )

        except Exception as exc:
            logger.debug(f"[FeedbackLoop] action pattern error: {exc}")

    async def _on_incident_event(self, data: dict, subject: str) -> None:
        """Track incoming incidents to detect re-appearance of healed clusters."""
        try:
            # Extract incident_id from NATS subject: nexus.incidents.<agent>.<signal>
            # The event payload includes correlation_id which IS the cluster_id
            incident_id = data.get("correlation_id") or data.get("incident_id")
            if incident_id:
                self._recent_incidents[incident_id] = time.monotonic()

                # If this was pending verification, it's a re-fire → mark failed
                if incident_id in self._pending_verifications:
                    pv = self._pending_verifications.pop(incident_id)
                    await self._kb.record_incident_outcome(
                        incident_id=incident_id,
                        runbook_id=pv.runbook_id,
                        action_type=pv.action_type,
                        resolved=False,  # re-fired → action didn't resolve
                        reason="re-fired",
                    )
                    logger.debug(
                        f"[FeedbackLoop] Incident {incident_id} re-fired — "
                        f"verification FAILED (runbook={pv.runbook_id})"
                    )

        except Exception as exc:
            logger.debug(f"[FeedbackLoop] incident tracking error: {exc}")

    async def stop(self) -> None:
        """Graceful shutdown."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info(f"[FeedbackLoop] Stopped — " f"cycles_run={self._cycles_run}")

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
        all_stats: dict[str, RunbookStats] = await self._store.get_all_runbook_stats(
            days=self._window_days
        )
        system_kpis: SystemKPIs = await self._store.get_system_kpis(
            days=self._window_days
        )

        if not all_stats:
            logger.info(
                "[FeedbackLoop] No healing records in window — skipping adjustments"
            )
        else:
            logger.info(
                f"[FeedbackLoop] Found stats for {len(all_stats)} runbook(s) — "
                f"total_actions={system_kpis.total_actions} "
                f"success_rate={system_kpis.autonomous_success_rate:.0%}"
            )

        # ── 2. Persist KnowledgeBase adjustments ──────────────────────────────
        adjustments: dict[str, float] = {}
        if all_stats:
            adjustments = await self._kb.bulk_update(all_stats)

        # ── 3. Push boosts to ConfidenceScorer ───────────────────────────────
        all_adjustments = await self._kb.get_all_adjustments()
        self._scorer.set_historical_boosts(all_adjustments)

        # ── 4. Run RunbookAdvisor ─────────────────────────────────────────────
        recs: list[RunbookRecommendation] = self._advisor.analyze(
            all_stats, system_kpis
        )

        # Also check chronic targets (async)
        chronic = await self._advisor.find_chronic_targets()
        recs.extend(chronic)

        # ── 5. Update signal-pattern records ─────────────────────────────────
        await self._update_signal_patterns()

        # ── 5b. Verify incident resolutions (P3b) ─────────────────────────────
        await self._verify_pending_incidents()

        # ── 4b. Record PPA prediction accuracy ────────────────────────────────
        await self._update_ppa_outcomes()

        # ── 6. Publish NATS event ─────────────────────────────────────────────
        cycle_ms = int((time.monotonic() - cycle_start) * 1000)
        self._last_kpis = system_kpis.to_dict()
        self._last_recs = [r.to_dict() for r in recs]
        self._last_cycle_at = time.monotonic()

        await self._publish_summary(system_kpis, recs, adjustments, cycle_ms)

        logger.info(
            f"[FeedbackLoop] Cycle #{self._cycles_run} complete in {cycle_ms}ms — "
            f"adjustments={len(adjustments)} "
            f"recommendations={len(recs)}"
        )

    async def _update_signal_patterns(self) -> None:
        """No-op placeholder kept for backwards compatibility.

        Real signal_type → runbook_id patterns are now recorded inline by
        :meth:`_on_action_event` (subscribed to nexus.actions.* in start()).
        The OutcomeRecord table only stores runbook_id so the audit-trail
        reverse-infer approach could never be exhaustive.
        """
        return

    async def _update_ppa_outcomes(self) -> None:
        """
        Flush PPA outcome batch to JSONL for PPA retraining pipeline.
        Nightly — calls run_batch_flush() on the wired PpaOutcomeTracker.
        """
        if self._ppa_tracker is not None:
            await self._ppa_tracker.run_batch_flush()

    async def _publish_summary(
        self,
        kpis: SystemKPIs,
        recs: list[RunbookRecommendation],
        adjustments: dict[str, float],
        cycle_ms: int,
    ) -> None:
        """Publish a LEARNING_CYCLE_COMPLETE event to NATS."""
        if not self._nats:
            return
        try:
            evt = IncidentEvent(
                agent=AgentType.ORCHESTRATOR,
                signal_type=SignalType.ANOMALY_DETECTED,  # Closest available type
                severity=Severity.INFO,
                context={
                    "type": "learning_cycle_complete",
                    "cycle_number": self._cycles_run,
                    "cycle_ms": cycle_ms,
                    "system_kpis": kpis.to_dict(),
                    "adjustments": {k: round(v, 4) for k, v in adjustments.items()},
                    "recommendations": [r.to_dict() for r in recs[:10]],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
            await self._nats.publish(evt)
        except Exception as exc:
            logger.debug(f"[FeedbackLoop] NATS publish failed: {exc}")

    # ── Per-incident verification (P3b) ────────────────────────────────────────

    async def _verify_pending_incidents(self) -> None:
        """
        Check pending incident verifications. If an incident hasn't re-fired
        within the verify_window_s after its action, mark it as RESOLVED.
        If it re-fired, it was already handled in _on_incident_event.
        """
        now = time.monotonic()
        expired: list[str] = []

        for incident_id, pv in self._pending_verifications.items():
            elapsed = now - pv.action_time
            if elapsed >= self._verify_window_s:
                # Incident didn't re-fire within the window → verified resolved
                expired.append(incident_id)
                await self._kb.record_incident_outcome(
                    incident_id=incident_id,
                    runbook_id=pv.runbook_id,
                    action_type=pv.action_type,
                    resolved=True,
                    reason="verified",
                )
                logger.debug(
                    f"[FeedbackLoop] Incident {incident_id} verified RESOLVED "
                    f"after {self._verify_window_s}s (runbook={pv.runbook_id})"
                )

        for iid in expired:
            self._pending_verifications.pop(iid, None)

        if expired:
            logger.info(
                f"[FeedbackLoop] Verification check: {len(expired)} incident(s) resolved, "
                f"{len(self._pending_verifications)} still pending"
            )

    # ── Manual trigger ────────────────────────────────────────────────────────

    async def run_now(self) -> dict[str, Any]:
        """
        Trigger an immediate learning cycle (useful for testing or CLI invocation).
        Returns the cycle summary.
        """
        await self._update_cycle()
        return {
            "cycles_run": self._cycles_run,
            "last_kpis": self._last_kpis,
            "last_recs": self._last_recs,
        }

    # ── Observability ─────────────────────────────────────────────────────────

    @property
    def status(self) -> dict[str, Any]:
        uptime = time.monotonic() - self._start_time if self._start_time else 0.0
        return {
            "cycles_run": self._cycles_run,
            "uptime_seconds": round(uptime, 1),
            "interval_s": self._interval,
            "window_days": self._window_days,
            "last_kpis": self._last_kpis,
            "last_recommendations": len(self._last_recs),
        }

# Factory
async def build_feedback_loop(
    confidence_scorer: ConfidenceScorer,
    nats_client: NATSClient | None = None,
    ppa_outcome_tracker: PpaOutcomeTracker | None = None,
    audit_db_path: str | None = None,
    knowledge_db_path: str | None = None,
    outcome_store: OutcomeStore | None = None,
    knowledge_base: KnowledgeBase | None = None,
    interval_s: float = 300.0,
) -> FeedbackLoop:
    """
    Build and initialize a FeedbackLoop with its dependencies.

    Args:
        confidence_scorer:   Phase 4 ConfidenceScorer (will receive historical boosts).
        nats_client:        Optional NATSClient for publishing learning updates.
        ppa_outcome_tracker: Optional PpaOutcomeTracker for PPA prediction tracking.
        audit_db_path:      Override for AuditTrail DB path (used only when
                            outcome_store is not provided).
        knowledge_db_path:  Override for KnowledgeBase DB path (used only when
                            knowledge_base is not provided).
        outcome_store:      Provide to share an already-connected OutcomeStore.
                            Avoids opening two SQLite handles to the same file when
                            the caller needs the store on NexusContext too.
        knowledge_base:     Provide to share an already-initialized KnowledgeBase.
        interval_s:         Polling interval (default 300s / 5 min).

    Returns:
        Initialized FeedbackLoop (not yet started — call loop.start()).
    """
    if outcome_store is None:
        store = OutcomeStore(db_path=audit_db_path)
        await store.connect()
    else:
        store = outcome_store

    if knowledge_base is None:
        kb = KnowledgeBase(db_path=knowledge_db_path)
        await kb.initialize()
    else:
        kb = knowledge_base

    return FeedbackLoop(
        outcome_store=store,
        knowledge_base=kb,
        confidence_scorer=confidence_scorer,
        nats_client=nats_client,
        ppa_outcome_tracker=ppa_outcome_tracker,
        interval_s=interval_s,
        verify_window_s=60.0,
    )
