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
import uuid
from datetime import datetime, timezone
from typing import Any

from nexus.bus.incident_event import AgentType, IncidentEvent, Severity, SignalType
from nexus.bus.nats_client import NATSClient
from nexus.engine.fsm import IncidentFSM, IncidentState
from nexus.governance.cooldown_store import CooldownStore
from nexus.governance.runbook_executor import RunbookExecutor
from nexus.reasoning.confidence_scorer import ConfidenceScorer
from nexus.reasoning.event_correlator import EventCorrelator
from nexus.reasoning.incident_cluster import IncidentCluster
from nexus.reasoning.rca_engine import RCAEngine, RCAResult
from nexus.reasoning.rca_validator import RCAValidator, ValidationVerdict, downgrade_rca

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
        db_client:          Database client for FSM and incident persistence.
    """

    def __init__(
        self,
        nats_client: NATSClient,
        correlator: EventCorrelator,
        rca_engine: RCAEngine,
        confidence_scorer: ConfidenceScorer,
        executor: RunbookExecutor,
        flush_interval_s: float = 30.0,
        max_concurrent: int = 5,
        dry_run: bool = False,
        db_client: Any = None,
        rca_validator: RCAValidator | None = None,
    ):
        self.nats = nats_client
        self.correlator = correlator
        self.rca = rca_engine
        self.scorer = confidence_scorer
        self.executor = executor
        self._flush_interval = flush_interval_s
        self._dry_run = dry_run
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self.db_client = db_client
        self._rca_validator: RCAValidator = rca_validator or RCAValidator()

        # Active incident tracking per target resource (K8s and AWS)
        self._active_incidents: dict[str, dict[str, Any]] = {}

        # Wire executor callbacks for lifecycle sync
        self.executor.on_incident_resolved = self.notify_incident_resolved
        self.executor.on_incident_unsolved = self.notify_incident_unsolved

        # Observability
        self._clusters_processed = 0
        self._actions_dispatched = 0
        self._rca_results: list[dict[str, Any]] = (
            []
        )  # Last 100 RCA results for inspection
        self._start_time: float | None = None

        # Background task handles
        self._flush_task: asyncio.Task | None = None

    def notify_incident_resolved(self, target: str, incident_id: str) -> None:
        """Called when RunbookExecutor verifies SLO post-checks passed."""
        active = self._active_incidents.get(target)
        if active and (active.get("incident_id") == incident_id or incident_id in (active.get("incident_id"), active.get("last_cluster_id"))):
            logger.info(
                f"[Orchestrator] Incident {incident_id} on target '{target}' RESOLVED — clearing active state"
            )
            self._active_incidents.pop(target, None)

    def notify_incident_unsolved(self, target: str, incident_id: str) -> None:
        """Called when RunbookExecutor post-checks fail — marks incident for retry."""
        active = self._active_incidents.get(target)
        if active and (active.get("incident_id") == incident_id or incident_id in (active.get("incident_id"), active.get("last_cluster_id"))):
            logger.warning(
                f"[Orchestrator] Incident {incident_id} on target '{target}' UNSOLVED — marked for retry"
            )
            active["fsm"]._current_state = IncidentState.RETRYING

    async def notify_incident_rejected(self, target: str, incident_id: str, reason: str = "") -> None:
        """Called when an operator rejects a pending approval action."""
        active = self._active_incidents.pop(target, None)
        if active:
            fsm = active.get("fsm")
            if fsm and fsm.can_transition_to(IncidentState.REJECTED):
                await fsm.transition_to(
                    IncidentState.REJECTED,
                    reason=reason or "Human operator rejected proposed remediation action",
                )
            logger.info(
                f"[Orchestrator] Incident {incident_id} on target '{target}' REJECTED — cleared active state"
            )

    async def _is_target_in_cooldown(self, runbook_id: str, target: str) -> bool:
        """Safely check cooldown across real CooldownStore, AsyncMock, or MagicMock."""
        ladder = getattr(self.executor, "ladder", None)
        cooldown_store = getattr(ladder, "_cooldown", None)
        if cooldown_store is not None and hasattr(cooldown_store, "is_in_cooldown"):
            try:
                key = CooldownStore.make_key(runbook_id, target)
                res = cooldown_store.is_in_cooldown(key)
                if asyncio.iscoroutine(res):
                    return bool(await res)
                if isinstance(res, bool):
                    return res
            except Exception:
                pass
        return False

    # Lifecycle
    async def start(self) -> None:
        """
        Subscribe to NATS and start the periodic flush loop.
        Returns after subscription is established (non-blocking).
        """
        self._start_time = time.monotonic()

        await self.nats.subscribe(
            handler=self._on_event,
            agent_filter=">",  # All agents
            # No durable_name: ephemeral consumer per-pod.
            # Durable push consumers are exclusive (one active subscriber) —
            # during a rolling deploy the new pod would collide with the old pod's
            # consumer and lose or duplicate messages.
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

    # NATS event handler
    async def _on_event(self, event: IncidentEvent) -> None:
        """
        NATS subscription handler. Ingest each event into the correlator.
        Fast path — never blocks.
        """
        # Anti-loop: ignore events emitted by NEXUS itself
        if str(event.agent).lower() == "orchestrator":
            return

        # Staleness filter: drop events older than 120s (e.g. replayed from NATS or delayed consumer lag)
        if event.timestamp:
            event_age_s = (datetime.now(timezone.utc) - event.timestamp).total_seconds()
            if event_age_s > 120.0:
                logger.debug(
                    f"[Orchestrator] Dropping stale event {event.event_id} "
                    f"({event.signal_type} on {event.resource_name}, age={event_age_s:.1f}s)"
                )
                return

        cluster = self.correlator.ingest(event)
        if cluster:
            # Schedule async processing without blocking the NATS handler
            asyncio.create_task(
                self._safe_process(cluster),
                name=f"process-{cluster.cluster_id}",
            )

    # Flush loop
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

    # Cluster processing
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
            1. Target Resolution & Incident Correlation / Deduplication
            2. RCA — the ONE LLM call (Gemini/OpenAI → rule-based fallback)
            3. Confidence calibration & FSM transitions
            4. Publish ORCHESTRATOR_DECISION event to NATS
            5. Remediation — LLM RCA staged for human approval; rule-based RCA
               routed through the governed RunbookExecutor (unless dry_run).
        """
        self._clusters_processed += 1

        # Determine target resource and environment (supports both K8s and AWS)
        primary = cluster.primary_resource or (cluster.events[0].resource_name if cluster.events else "unknown")
        ns = cluster.namespace or (cluster.events[0].namespace if cluster.events else "default")
        target = f"{ns}/{primary}"

        # Detect environment: K8s or AWS
        is_aws = any(
            str(getattr(e, "agent", "")).lower() in ("lambda", "apigw", "sqs", "dynamodb", "cloudwatch")
            or "aws" in str(getattr(e, "namespace", "")).lower()
            or str(getattr(e, "resource_name", "")).startswith("arn:aws:")
            for e in cluster.events
        )
        environment = "aws" if is_aws else "kubernetes"

        # Check active incident deduplication and retry state
        active = self._active_incidents.get(target)
        incident_id: str
        fsm: IncidentFSM

        if active is not None and not active["fsm"].is_terminal():
            fsm = active["fsm"]
            current_state = fsm.current_state

            # If an action/approval is already in flight for this target, consolidate signals without re-diagnosing
            if current_state in (
                IncidentState.DETECTED,
                IncidentState.CORRELATED,
                IncidentState.DIAGNOSING,
                IncidentState.PLANNING,
                IncidentState.POLICY_CHECK,
                IncidentState.APPROVAL_PENDING,
                IncidentState.EXECUTING,
                IncidentState.VERIFYING,
            ):
                logger.info(
                    f"[Orchestrator] Target '{target}' already has active incident {active['incident_id']} "
                    f"in state={current_state.value} — consolidating signals, suppressing duplicate analysis"
                )
                return

            # If incident is in RETRYING state, increment retry count and evaluate limits
            if current_state == IncidentState.RETRYING:
                active["retry_count"] += 1
                incident_id = active["incident_id"]
                logger.info(
                    f"[Orchestrator] Incident {incident_id} retrying for '{target}' "
                    f"(attempt {active['retry_count']}/{active['max_retries']})"
                )
                if active["retry_count"] > active["max_retries"]:
                    logger.warning(
                        f"[Orchestrator] Max retries ({active['max_retries']}) exceeded for '{target}' "
                        f"— escalating incident {incident_id} to human operator"
                    )
                    await fsm.transition_to(
                        IncidentState.ESCALATED,
                        reason=f"Max retries ({active['max_retries']}) exceeded without resolution",
                    )
                    if self.nats:
                        try:
                            await self.nats.publish_raw(
                                "nexus.alerts.escalated",
                                {
                                    "incident_id": incident_id,
                                    "target": target,
                                    "environment": environment,
                                    "retries": active["retry_count"],
                                    "reason": f"Autonomous remediation failed to restore health after {active['max_retries']} attempts",
                                },
                            )
                        except Exception:
                            pass
                    return

                # Retry limit not reached: transition RETRYING -> PLANNING under SAME incident ID
                await fsm.transition_to(
                    IncidentState.PLANNING,
                    reason=f"Retrying remediation attempt {active['retry_count']}/{active['max_retries']}",
                )
            else:
                incident_id = active["incident_id"]
        else:
            # Create fresh incident for target and register synchronously BEFORE any await point
            incident_id = str(uuid.uuid4())
            fsm = IncidentFSM(
                incident_id=incident_id,
                current_state=IncidentState.DETECTED,
                db_client=self.db_client,
                nats_client=self.nats,
            )
            self._active_incidents[target] = {
                "incident_id": incident_id,
                "fsm": fsm,
                "target": target,
                "environment": environment,
                "retry_count": 0,
                "max_retries": 3,
                "created_at": datetime.now(timezone.utc),
                "last_cluster_id": cluster.cluster_id,
            }

            if self.db_client:
                try:
                    fingerprint = (
                        getattr(cluster, "fingerprint", None)
                        or f"{cluster.namespace or 'default'}:{cluster.primary_resource or cluster.cluster_id}"
                    )
                    await self.db_client.create_incident(
                        fingerprint=fingerprint,
                        environment=environment,
                        target_resource=target,
                        severity=cluster.highest_severity or "error",
                        trigger_source=str(cluster.events[0].agent) if cluster.events else "orchestrator",
                        trigger_payload={"cluster_id": cluster.cluster_id, "summary": cluster.to_summary()},
                        incident_id=incident_id,
                    )
                except Exception as db_err:
                    logger.warning(f"[Orchestrator] Failed to persist new incident to DB: {db_err}")

            await fsm.transition_to(
                IncidentState.CORRELATED,
                reason=f"Correlated {len(cluster.events)} signals into cluster {cluster.cluster_id}",
            )

        logger.info(
            f"[Orchestrator] Processing {cluster.cluster_id} (incident={incident_id}) — "
            f"{len(cluster.events)} events, target={target}, env={environment}, "
            f"severity={cluster.highest_severity}"
        )

        # ── Step 1: RCA ───────────────────────────────────────────────────────
        if fsm.can_transition_to(IncidentState.DIAGNOSING):
            await fsm.transition_to(IncidentState.DIAGNOSING, reason="Starting RCA analysis")
        rca_result = await self.rca.analyze(cluster)

        # ── Step 1b: Validate RCA — consistency + evidence gates ─────────────
        # Only LLM-sourced RCA is validated; rule-based is trusted as-is.
        validation_verdict: ValidationVerdict = self._rca_validator.validate(
            cluster, rca_result
        )
        if validation_verdict.block_reason:
            # LLM made an inference leap with no evidentiary support — demote to L0.
            logger.warning(
                f"[Orchestrator] RCA validation BLOCKED for {cluster.cluster_id} "
                f"(incident={incident_id}): {validation_verdict.block_reason} "
                f"— downgrading to unknown/L0"
            )
            rca_result = downgrade_rca(rca_result, validation_verdict.block_reason)

        # ── Step 2: Confidence calibration ───────────────────────────────────
        confidence = self.scorer.score(
            cluster,
            rca_result,
            external_penalty=abs(validation_verdict.confidence_delta),
        )
        max_level = self.scorer.gate(confidence)
        effective_level = min(rca_result.healing_level, max_level)

        if fsm.can_transition_to(IncidentState.PLANNING):
            await fsm.transition_to(IncidentState.PLANNING, reason=f"RCA diagnosed {rca_result.failure_class}")
        if fsm.can_transition_to(IncidentState.POLICY_CHECK):
            await fsm.transition_to(IncidentState.POLICY_CHECK, reason="Evaluating governance policies")

        logger.info(
            f"[Orchestrator] RCA complete for {cluster.cluster_id} (incident={incident_id}): "
            f"class={rca_result.failure_class} "
            f"suggested_L{rca_result.healing_level} "
            f"→ effective_L{effective_level} "
            f"confidence={self.scorer.describe(confidence)} "
            f"runbook={rca_result.runbook_id} "
            f"src={rca_result.source}"
        )

        # ── Step 3: Record + publish decision ────────────────────────────────
        rca_record = {
            "cluster_id": cluster.cluster_id,
            "incident_id": incident_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "rca": rca_result.to_dict(),
            "confidence": round(confidence, 3),
            "effective_level": effective_level,
            "cluster_summary": cluster.to_summary(),
            "validation": validation_verdict.to_dict(),
        }
        self._rca_results.append(rca_record)
        if len(self._rca_results) > 100:
            self._rca_results = self._rca_results[-100:]

        # Publish ORCHESTRATOR_DECISION to NATS (for external audit/dashboards)
        await self._publish_decision_event(
            cluster, rca_result, confidence, effective_level
        )

        # ── Step 4: Route to remediation (ONE LLM call per incident) ──────────────
        # Check cooldown first
        if rca_result.runbook_id:
            if await self._is_target_in_cooldown(rca_result.runbook_id, target):
                logger.info(
                    f"[Orchestrator] Target '{target}' is in cooldown for {rca_result.runbook_id} — suppressing remediation"
                )
                return

        if rca_result.source in ("gemini", "openai"):
            # Blocked diagnoses (unsupported inference leaps / hallucinations) must never queue approvals
            if validation_verdict.block_reason:
                logger.info(
                    f"[Orchestrator] LLM RCA for {cluster.cluster_id} BLOCKED by validator: "
                    f"{validation_verdict.block_reason} — alert only, no approval queued"
                )
                return

            if effective_level == 0 and rca_result.runbook_id:
                logger.info(
                    f"[Orchestrator] LLM RCA for {cluster.cluster_id} "
                    f"→ effective_level=0 after confidence gate "
                    f"(raw_conf={rca_result.confidence:.2f}, "
                    f"calibrated={confidence:.2f}) — alert only, no approval queued"
                )
                return
            self._actions_dispatched += 1
            await self._stage_llm_remediation(
                cluster, rca_result, confidence, effective_level,
                incident_id=incident_id, target=target, fsm=fsm,
            )
            return

        # Rule-based RCA — governed RunbookExecutor path
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
        primary_event = self._build_enriched_event(cluster, rca_result, confidence, incident_id=incident_id)
        self.executor.confidence = confidence

        if fsm.can_transition_to(IncidentState.EXECUTING):
            await fsm.transition_to(
                IncidentState.EXECUTING,
                reason=f"Dispatching autonomous runbook {rca_result.runbook_id}",
            )

        self._actions_dispatched += 1
        await self.executor.handle_event(primary_event)

    # Event helpers
    def _build_enriched_event(
        self,
        cluster: IncidentCluster,
        rca: RCAResult,
        confidence: float,
        incident_id: str | None = None,
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
                "root_cause": rca.root_cause,
                "failure_class": rca.failure_class,
                "reasoning": rca.reasoning,
                "source": rca.source,
                "cluster_id": cluster.cluster_id,
                "incident_id": incident_id or cluster.cluster_id,
            },
        }

        return IncidentEvent(
            agent=primary.agent,
            signal_type=primary.signal_type,
            severity=primary.severity,
            namespace=cluster.namespace or primary.namespace,
            resource_name=cluster.primary_resource or primary.resource_name,
            resource_kind=primary.resource_kind,
            deploy_sha=primary.deploy_sha,
            correlation_id=incident_id or cluster.cluster_id,
            context=enriched_context,
            suggested_runbook=rca.runbook_id,
            suggested_healing_level=rca.healing_level,
            confidence=confidence,
        )

    async def _stage_llm_remediation(
        self,
        cluster: IncidentCluster,
        rca: RCAResult,
        confidence: float,
        effective_level: int,
        incident_id: str | None = None,
        target: str | None = None,
        fsm: IncidentFSM | None = None,
    ) -> None:
        """Stage an LLM-sourced RCA's remediation for human approval — no LLM call.

        ``RCAEngine.analyze`` already produced the root cause and a suggested
        runbook; re-asking the model (the old ``handle_cluster`` second call) is
        what doubled LLM traffic per incident. We resolve the suggested runbook
        in the library and enqueue the same (action, event) snapshot
        ``RunbookExecutor.execute_approved()`` re-dispatches on approval — so an
        approved LLM remediation still crosses the full governance plane
        (cooldown, circuit breaker, OPA, audit, rollback). No mapped runbook
        surfaces as manual review carrying the RCA diagnosis.
        """
        queue = self.executor.ladder.approval_queue
        runbook = (
            self.executor.library.get(rca.runbook_id) if rca.runbook_id else None
        )
        resolved_incident_id = incident_id or cluster.cluster_id

        if runbook is not None and runbook.actions:
            action = runbook.actions[0]
            event = self._build_enriched_event(cluster, rca, confidence, incident_id=resolved_incident_id)
            resolved_target = (
                target
                or f"{event.namespace or 'default'}/{event.resource_name or 'unknown'}"
            )
            # Check cooldown before staging
            if await self._is_target_in_cooldown(runbook.id, resolved_target):
                logger.info(
                    f"[Orchestrator] Target '{resolved_target}' is in cooldown for {runbook.id} — skipping approval staging"
                )
                return

            approval_id = queue.enqueue(
                runbook_id=runbook.id,
                action_type=action.type,
                target=resolved_target,
                incident_id=resolved_incident_id,
                healing_level=runbook.healing_level,
                confidence=confidence,
                context={
                    "event_id": event.event_id,
                    "signal_type": event.signal_type,
                    "namespace": event.namespace,
                    "resource": event.resource_name,
                    "action": action.model_dump(),
                    "event": event.model_dump(),
                    "blast_radius": runbook.blast_radius,
                    "rca_source": rca.source,
                    "effective_level": effective_level,
                    "rca": rca.to_dict(),
                },
            )
            if fsm and fsm.can_transition_to(IncidentState.APPROVAL_PENDING):
                await fsm.transition_to(
                    IncidentState.APPROVAL_PENDING,
                    reason=f"Staged {runbook.id} (approval_id={approval_id}) for human sign-off",
                )
            logger.info(
                f"[Orchestrator] Staged LLM RCA remediation for approval: "
                f"approval_id={approval_id} runbook={runbook.id} target={resolved_target} "
                f"L{runbook.healing_level} confidence={confidence:.2f} "
                f"(single LLM call)"
            )
        else:
            resolved_target = (
                target
                or f"{cluster.namespace or 'default'}/{cluster.primary_resource or 'unknown'}"
            )
            approval_id = queue.enqueue(
                runbook_id="llm_dynamic",
                action_type="manual_review",
                target=resolved_target,
                incident_id=resolved_incident_id,
                healing_level=0,
                confidence=confidence,
                context={
                    "cluster_summary": cluster.to_summary(),
                    "llm_diagnosis": rca.root_cause,
                    "rca_source": rca.source,
                    "effective_level": effective_level,
                },
            )
            if fsm and fsm.can_transition_to(IncidentState.APPROVAL_PENDING):
                await fsm.transition_to(
                    IncidentState.APPROVAL_PENDING,
                    reason="Staged manual review for human sign-off",
                )
            logger.info(
                f"[Orchestrator] No mapped runbook for LLM RCA {cluster.cluster_id} "
                f"→ staged for manual review (class={rca.failure_class}, "
                f"confidence={confidence:.2f})"
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
                agent=AgentType.ORCHESTRATOR,
                signal_type=SignalType.THRESHOLD_BREACH,  # Closest fitting type
                severity=(
                    Severity.CRITICAL
                    if cluster.highest_severity == "critical"
                    else Severity.WARNING
                ),
                namespace=cluster.namespace,
                resource_name=cluster.primary_resource,
                correlation_id=cluster.cluster_id,
                context={
                    "type": "orchestrator_decision",
                    "cluster_id": cluster.cluster_id,
                    "failure_class": rca.failure_class,
                    "root_cause": rca.root_cause[:200],
                    "healing_level": effective_level,
                    "runbook_id": rca.runbook_id,
                    "confidence": round(confidence, 3),
                    "rca_source": rca.source,
                    "signal_count": len(cluster.events),
                    "agent_count": len(cluster.agent_types),
                    "actions_to_avoid": rca.actions_to_avoid,
                },
                confidence=confidence,
            )
            await self.nats.publish(decision_event)
        except Exception as exc:
            logger.debug(f"[Orchestrator] Failed to publish decision event: {exc}")

    # ── Observability ─────────────────────────────────────────────────────────

    @property
    def status(self) -> dict:
        uptime = time.monotonic() - self._start_time if self._start_time else 0.0
        return {
            "uptime_seconds": round(uptime, 1),
            "clusters_processed": self._clusters_processed,
            "actions_dispatched": self._actions_dispatched,
            "correlator_stats": self.correlator.stats,
            "rca_stats": self.rca.stats,
            "governance_cb": self.executor.ladder.governance_cb.status_dict(),
        }

    def last_rca_results(self, n: int = 10) -> list[dict[str, Any]]:
        """Return the N most recent RCA records (newest first)."""
        return list(reversed(self._rca_results[-n:]))

# Factory

def build_orchestrator(
    nats_client: NATSClient,
    executor: RunbookExecutor,
    gemini_api_key: str | None = None,
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
        gemini_api_key:       Google AI API key (reads NEXUS_LLM_API_KEY if None).
        correlation_window_s: EventCorrelator time window (default 60s).
        quorum_events:        Events needed to form a cluster (default 3).
        flush_interval_s:     Stale cluster flush interval (default 30s).
        dry_run:              Don't call executor — only log decisions.
    """
    correlator = EventCorrelator(
        correlation_window_s=correlation_window_s,
        quorum_events=quorum_events,
        flush_timeout_s=flush_interval_s * 1.5,
    )
    rca_engine = RCAEngine(api_key=gemini_api_key)
    scorer = ConfidenceScorer()

    return NexusOrchestrator(
        nats_client=nats_client,
        correlator=correlator,
        rca_engine=rca_engine,
        confidence_scorer=scorer,
        executor=executor,
        flush_interval_s=flush_interval_s,
        dry_run=dry_run,
    )
