"""NEXUS Feedback Loop - closes act-verify-learn cycle."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from nexus.bus.nats_client import NATSClient
from nexus.learning.feedback_factory import build_feedback_loop
from nexus.learning.feedback_publisher import publish_learning_cycle_summary
from nexus.learning.feedback_verifier import (
    PendingIncidentVerification,
    handle_action_event,
    handle_incident_event,
    handle_lifecycle_event,
    verify_pending_incidents,
)
from nexus.learning.knowledge_base import KnowledgeBase
from nexus.learning.outcome_store import OutcomeStore, SystemKPIs
from nexus.learning.ppa_outcome_tracker import PpaOutcomeTracker
from nexus.reasoning.confidence_scorer import ConfidenceScorer

logger = logging.getLogger(__name__)

__all__ = ["PendingIncidentVerification", "FeedbackLoop", "build_feedback_loop"]


class FeedbackLoop:
    """Background learning coordinator for the NEXUS system."""

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
        self._store = outcome_store
        self._kb = knowledge_base
        self._scorer = confidence_scorer
        self._nats = nats_client
        self._ppa_tracker = ppa_outcome_tracker
        self._interval = float(os.getenv("NEXUS_FEEDBACK_INTERVAL_S", interval_s))
        self._window_days = int(os.getenv("NEXUS_FEEDBACK_WINDOW_DAYS", window_days))
        self._verify_window_s = float(
            os.getenv("NEXUS_INCIDENT_VERIFY_WINDOW_S", verify_window_s)
        )
        self._advisor = None
        self._pending_verifications: dict[str, PendingIncidentVerification] = {}
        self._recent_incidents: dict[str, float] = {}
        self._task: asyncio.Task | None = None
        self._cycles_run = 0
        self._last_cycle_at: float | None = None
        self._last_kpis: dict[str, Any] | None = None
        self._last_recs: list[dict[str, Any]] = []
        self._start_time: float | None = None

    async def start(self) -> None:
        """Start the background polling loop (non-blocking)."""
        self._start_time = time.monotonic()
        self._task = asyncio.create_task(self._run(), name="nexus-feedback-loop")
        if self._nats is not None:
            subs = [
                ("nexus.actions.>", self._on_action_event),
                ("nexus.incidents.>", self._on_incident_event),
                ("nexus.lifecycle.>", self._on_lifecycle_event),
            ]
            for pattern, handler in subs:
                try:
                    await self._nats.subscribe_raw(pattern, handler)
                    logger.info(f"[FeedbackLoop] Subscribed to {pattern}")
                except Exception as exc:
                    logger.warning(
                        f"[FeedbackLoop] Subscription to {pattern} failed: {exc}"
                    )

        logger.info(
            f"[FeedbackLoop] Started — interval={self._interval}s "
            f"window={self._window_days}d verify_window={self._verify_window_s}s"
        )

    async def _on_action_event(self, data: dict, subject: str) -> None:
        await handle_action_event(data, self._kb, self._pending_verifications)

    async def _on_incident_event(self, data: dict, subject: str) -> None:
        await handle_incident_event(
            data, self._recent_incidents, self._pending_verifications, self._kb
        )

    async def _on_lifecycle_event(self, data: dict, subject: str) -> None:
        handle_lifecycle_event(
            data, subject, self._recent_incidents, self._pending_verifications
        )

    async def stop(self) -> None:
        """Graceful shutdown."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info(f"[FeedbackLoop] Stopped — cycles_run={self._cycles_run}")

    async def _run(self) -> None:
        """Background polling loop."""
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

    async def _update_cycle(self) -> None:
        """One full learning cycle."""
        cycle_start = time.monotonic()
        self._cycles_run += 1

        all_stats = await self._store.get_all_runbook_stats(days=self._window_days)
        system_kpis = await self._store.get_system_kpis(days=self._window_days)

        adjustments = await self._kb.bulk_update(all_stats) if all_stats else {}
        all_adjustments = await self._kb.get_all_adjustments()
        self._scorer.set_historical_boosts(all_adjustments)

        recs: list[Any] = []
        if self._advisor:
            recs = self._advisor.analyze(all_stats, system_kpis)
            chronic = await self._advisor.find_chronic_targets()
            recs.extend(chronic)

        await self._update_signal_patterns()
        await self._verify_pending_incidents()
        await self._update_ppa_outcomes()

        cycle_ms = int((time.monotonic() - cycle_start) * 1000)
        self._last_kpis = system_kpis.to_dict()
        self._last_recs = [r.to_dict() for r in recs]
        self._last_cycle_at = time.monotonic()
        await self._publish_summary(system_kpis, recs, adjustments, cycle_ms)

    async def _update_signal_patterns(self) -> None:
        return

    async def _update_ppa_outcomes(self) -> None:
        if self._ppa_tracker is not None:
            await self._ppa_tracker.run_batch_flush()

    async def _publish_summary(
        self,
        kpis: SystemKPIs,
        recs: list[Any],
        adjustments: dict[str, float],
        cycle_ms: int,
    ) -> None:
        await publish_learning_cycle_summary(
            self._nats, self._cycles_run, kpis, recs, adjustments, cycle_ms
        )

    async def _verify_pending_incidents(self) -> None:
        await verify_pending_incidents(
            self._pending_verifications, self._kb, self._verify_window_s
        )

    async def run_now(self) -> dict[str, Any]:
        """Trigger an immediate learning cycle."""
        await self._update_cycle()
        return {
            "cycles_run": self._cycles_run,
            "last_kpis": self._last_kpis,
            "last_recs": self._last_recs,
        }

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
