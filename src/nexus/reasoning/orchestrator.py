"""
NEXUS Orchestrator
===================
Central `sense → reason → act → verify → learn` controller.

The Orchestrator is the only NATS subscriber in production (Phase 4+).
It sits between the raw event bus and the Governance plane:

    Domain Agents → NATS → Orchestrator → RunbookExecutor → K8s API
                              ↕
                         RCA Engine (Gemini)
                         ConfidenceScorer
                         EventCorrelator

Lifecycle:
    1. start()        — subscribe to NATS incident stream, start flush loop
    2. _on_event()    — ingest each event into EventCorrelator
    3. _process()     — called when cluster is ready:
                          a. RCA (Gemini + fallback)
                          b. Confidence calibration
                          c. Publish ORCHESTRATOR_DECISION audit event
                          d. Call RunbookExecutor.handle_event() with enriched event
    4. _flush_loop()  — every 30s, emit stale clusters without quorum

Safety protections:
    • Semaphore (default 5) — limits concurrent cluster processing
    • Circuit breaker from ActionLadder — stops processing if healing is causing harm
    • Anti-loop: ignores events from AgentType.ORCHESTRATOR
    • Confidence gate: executor confidence is set from ConfidenceScorer output
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from nexus.bus.incident_event import AgentType, IncidentEvent, Severity, SignalType
from nexus.bus.nats_client import NATSClient
from nexus.governance.runbook_executor import RunbookExecutor
from nexus.reasoning.confidence_scorer import ConfidenceScorer
from nexus.reasoning.event_correlator import EventCorrelator
from nexus.reasoning.incident_cluster import IncidentCluster
from nexus.reasoning.rca_engine import RCAEngine, RCAResult

logger = logging.getLogger(__name__)


class NexusOrchestrator:
    """
    Central NEXUS reasoning controller.

    Args:
        nats_client:        Connected NATSClient (shared with executor).
        correlator:         EventCorrelator instance.
        rca_engine:         RCAEngine instance (Gemini + fallback).
        confidence_scorer:  ConfidenceScorer instance.
        executor:           RunbookExecutor (Phase 3, full governance).
        flush_interval_s:   How often to flush stale clusters (default 30s).
        max_concurrent:     Max concurrent cluster analyses (semaphore, default 5).
        dry_run:            If True, perform RCA but don't call executor.
    """

    def __init__(
        self,
        nats_client:       NATSClient,
        correlator:        EventCorrelator,
        rca_engine:        RCAEngine,
        confidence_scorer: ConfidenceScorer,
        executor:          RunbookExecutor,
        flush_interval_s:  float = 30.0,
        max_concurrent:    int = 5,
        dry_run:           bool = False,
    ):
        self.nats       = nats_client
        self.correlator = correlator
        self.rca        = rca_engine
        self.scorer     = confidence_scorer
        self.executor   = executor
        self._flush_interval = flush_interval_s
        self._dry_run   = dry_run
        self._semaphore = asyncio.Semaphore(max_concurrent)

        # Observability
        self._clusters_processed = 0
        self._actions_dispatched = 0
        self._rca_results: List[Dict[str, Any]] = []   # Last 100 RCA results for inspection
        self._start_time: Optional[float] = None

        # Background task handles
        self._flush_task:  Optional[asyncio.Task] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Subscribe to NATS and start the periodic flush loop.
        Returns after subscription is established (non-blocking).
        """
        self._start_time = time.monotonic()

        await self.nats.subscribe(
            handler      = self._on_event,
            agent_filter = ">",                             # All agents
            durable_name = "nexus-orchestrator-v4",
        )

        self._flush_task = asyncio.create_task(
            self._flush_loop(), name="orchestrator-flush"
        )
        logger.info(
            f"[Orchestrator] Started — "
            f"flush_interval={self._flush_interval}s "
            f"dry_run={self._dry_run}"
        )

    async def stop(self) -> None:
        """Graceful shutdown."""
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        logger.info(
            f"[Orchestrator] Stopped — "
            f"clusters_processed={self._clusters_processed} "
            f"actions_dispatched={self._actions_dispatched}"
        )

    # ── NATS event handler ────────────────────────────────────────────────────

    async def _on_event(self, event: IncidentEvent) -> None:
        """
        NATS subscription handler. Ingest each event into the correlator.
        Fast path — never blocks.
        """
        # Anti-loop: ignore events emitted by NEXUS itself
        if str(event.agent).lower() == "orchestrator":
            return

        cluster = self.correlator.ingest(event)
        if cluster:
            # Schedule async processing without blocking the NATS handler
            asyncio.create_task(
                self._safe_process(cluster),
                name=f"process-{cluster.cluster_id}",
            )

    # ── Flush loop ────────────────────────────────────────────────────────────

    async def _flush_loop(self) -> None:
        """Periodically flush stale clusters that never reached quorum."""
        while True:
            try:
                await asyncio.sleep(self._flush_interval)
                stale = self.correlator.flush_stale()
                for cluster in stale:
                    asyncio.create_task(
                        self._safe_process(cluster),
                        name=f"flush-{cluster.cluster_id}",
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"[Orchestrator] Flush loop error: {exc}")

    # ── Cluster processing ────────────────────────────────────────────────────

    async def _safe_process(self, cluster: IncidentCluster) -> None:
        """Wrapper that respects the concurrency semaphore and swallows exceptions."""
        async with self._semaphore:
            try:
                await self._process_cluster(cluster)
            except Exception as exc:
                logger.error(
                    f"[Orchestrator] Error processing {cluster.cluster_id}: {exc}",
                    exc_info=True,
                )

    async def _process_cluster(self, cluster: IncidentCluster) -> None:
        """
        Full Reason → Act cycle for one IncidentCluster:
            1. RCA (Gemini → rule-based fallback)
            2. Confidence calibration
            3. Publish ORCHESTRATOR_DECISION event to NATS
            4. Route enriched event to RunbookExecutor (unless dry_run)
        """
        self._clusters_processed += 1

        logger.info(
            f"[Orchestrator] Processing {cluster.cluster_id} — "
            f"{len(cluster.events)} events, "
            f"ns={cluster.namespace}, "
            f"severity={cluster.highest_severity}"
        )

        # ── Step 1: RCA ───────────────────────────────────────────────────────
        rca_result = await self.rca.analyze(cluster)

        # ── Step 2: Confidence calibration ───────────────────────────────────
        confidence  = self.scorer.score(cluster, rca_result)
        max_level   = self.scorer.gate(confidence)
        # Don't allow higher healing level than RCA suggested
        effective_level = min(rca_result.healing_level, max_level)

        logger.info(
            f"[Orchestrator] RCA complete for {cluster.cluster_id}: "
            f"class={rca_result.failure_class} "
            f"suggested_L{rca_result.healing_level} "
            f"→ effective_L{effective_level} "
            f"confidence={self.scorer.describe(confidence)} "
            f"runbook={rca_result.runbook_id} "
            f"src={rca_result.source}"
        )

        # ── Step 3: Record + publish decision ────────────────────────────────
        rca_record = {
            "cluster_id":       cluster.cluster_id,
            "timestamp":        datetime.now(timezone.utc).isoformat(),
            "rca":              rca_result.to_dict(),
            "confidence":       round(confidence, 3),
            "effective_level":  effective_level,
            "cluster_summary":  cluster.to_summary(),
        }
        self._rca_results.append(rca_record)
        if len(self._rca_results) > 100:
            self._rca_results = self._rca_results[-100:]

        # Publish ORCHESTRATOR_DECISION to NATS (for external audit/dashboards)
        await self._publish_decision_event(cluster, rca_result, confidence, effective_level)

        # ── Step 4: Route to RunbookExecutor ─────────────────────────────────
        if not rca_result.runbook_id and effective_level == 0:
            logger.info(
                f"[Orchestrator] L0 / no runbook for {cluster.cluster_id} "
                f"— alert dispatched, no autonomous action"
            )
            return

        if self._dry_run:
            logger.info(
                f"[Orchestrator] DRY RUN — would dispatch L{effective_level} "
                f"runbook={rca_result.runbook_id} for {cluster.cluster_id}"
            )
            return

        # Build an enriched event from the most critical signal in the cluster
        primary_event = self._build_enriched_event(cluster, rca_result, confidence)
        self.executor.confidence = confidence   # Update executor's confidence for this decision

        self._actions_dispatched += 1
        await self.executor.handle_event(primary_event)

    # ── Event helpers ─────────────────────────────────────────────────────────

    def _build_enriched_event(
        self,
        cluster: IncidentCluster,
        rca: RCAResult,
        confidence: float,
    ) -> IncidentEvent:
        """
        Build a primary IncidentEvent enriched with RCA metadata.
        We use the most critical signal from the cluster as the base event
        so that RunbookLibrary.find_matching() can still work by signal_type.
        """
        primary = cluster.most_critical_event or cluster.events[0]

        # Inject RCA context into the event
        enriched_context = {
            **(primary.context if isinstance(primary.context, dict) else {}),
            "_rca": {
                "root_cause":    rca.root_cause,
                "failure_class": rca.failure_class,
                "reasoning":     rca.reasoning,
                "source":        rca.source,
                "cluster_id":    cluster.cluster_id,
            },
        }

        return IncidentEvent(
            agent                  = primary.agent,
            signal_type            = primary.signal_type,
            severity               = primary.severity,
            namespace              = cluster.namespace or primary.namespace,
            resource_name          = cluster.primary_resource or primary.resource_name,
            resource_kind          = primary.resource_kind,
            deploy_sha             = primary.deploy_sha,
            correlation_id         = cluster.cluster_id,
            context                = enriched_context,
            suggested_runbook      = rca.runbook_id,
            suggested_healing_level = rca.healing_level,
            confidence             = confidence,
        )

    async def _publish_decision_event(
        self,
        cluster: IncidentCluster,
        rca: RCAResult,
        confidence: float,
        effective_level: int,
    ) -> None:
        """Publish an ORCHESTRATOR_DECISION event to NATS for observability."""
        try:
            decision_event = IncidentEvent(
                agent       = AgentType.ORCHESTRATOR,
                signal_type = SignalType.THRESHOLD_BREACH,   # Closest fitting type
                severity    = Severity.CRITICAL if cluster.highest_severity == "critical" else Severity.WARNING,
                namespace   = cluster.namespace,
                resource_name = cluster.primary_resource,
                correlation_id = cluster.cluster_id,
                context = {
                    "type":                 "orchestrator_decision",
                    "cluster_id":           cluster.cluster_id,
                    "failure_class":        rca.failure_class,
                    "root_cause":           rca.root_cause[:200],
                    "healing_level":        effective_level,
                    "runbook_id":           rca.runbook_id,
                    "confidence":           round(confidence, 3),
                    "rca_source":           rca.source,
                    "signal_count":         len(cluster.events),
                    "agent_count":          len(cluster.agent_types),
                    "actions_to_avoid":     rca.actions_to_avoid,
                },
                confidence = confidence,
            )
            await self.nats.publish(decision_event)
        except Exception as exc:
            logger.debug(f"[Orchestrator] Failed to publish decision event: {exc}")

    # ── Observability ─────────────────────────────────────────────────────────

    @property
    def status(self) -> dict:
        uptime = time.monotonic() - self._start_time if self._start_time else 0.0
        return {
            "uptime_seconds":     round(uptime, 1),
            "clusters_processed": self._clusters_processed,
            "actions_dispatched": self._actions_dispatched,
            "correlator_stats":   self.correlator.stats,
            "rca_stats":          self.rca.stats,
            "governance_cb":      self.executor.ladder.governance_cb.status_dict(),
        }

    def last_rca_results(self, n: int = 10) -> List[Dict[str, Any]]:
        """Return the N most recent RCA records (newest first)."""
        return list(reversed(self._rca_results[-n:]))


# ──────────────────────────────────────────────────────────────────────────────
# Factory
# ──────────────────────────────────────────────────────────────────────────────

def build_orchestrator(
    nats_client: NATSClient,
    executor: RunbookExecutor,
    gemini_api_key: Optional[str] = None,
    correlation_window_s: float = 60.0,
    quorum_events: int = 3,
    flush_interval_s: float = 30.0,
    dry_run: bool = False,
) -> NexusOrchestrator:
    """
    Build a fully-configured NexusOrchestrator with default component settings.

    Args:
        nats_client:          Connected NATSClient.
        executor:             Phase 3 RunbookExecutor.
        gemini_api_key:       Google AI API key (reads NEXUS_GEMINI_API_KEY if None).
        correlation_window_s: EventCorrelator time window (default 60s).
        quorum_events:        Events needed to form a cluster (default 3).
        flush_interval_s:     Stale cluster flush interval (default 30s).
        dry_run:              Don't call executor — only log decisions.
    """
    correlator = EventCorrelator(
        correlation_window_s = correlation_window_s,
        quorum_events        = quorum_events,
        flush_timeout_s      = flush_interval_s * 1.5,
    )
    rca_engine = RCAEngine(api_key=gemini_api_key)
    scorer     = ConfidenceScorer()

    return NexusOrchestrator(
        nats_client       = nats_client,
        correlator        = correlator,
        rca_engine        = rca_engine,
        confidence_scorer = scorer,
        executor          = executor,
        flush_interval_s  = flush_interval_s,
        dry_run           = dry_run,
    )
