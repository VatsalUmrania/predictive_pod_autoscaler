"""
NEXUS Unified LangGraph Incident Workflow
=========================================
Builds, compiles, and orchestrates the governed incident response StateGraph.
Replaces basic ReAct loops with a deterministic Plan-Validate-Execute-Verify-Rollback
state machine integrated with PostgreSQL/Memory checkpointers.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from nexus.engine.fsm import IncidentState
from nexus.graph.checkpointer import GraphCheckpointStore
from nexus.graph.nodes import (
    approval_node,
    collect_telemetry_node,
    diagnose_node,
    escalate_node,
    execute_node,
    govern_node,
    learn_node,
    plan_remediation_node,
    rollback_node,
    triage_node,
    verify_node,
)
from nexus.graph.state import IncidentGraphState

logger = logging.getLogger(__name__)


# ── Conditional Routing Functions ─────────────────────────────────────────────

def route_after_govern(state: IncidentGraphState) -> Literal["execute", "approval", "escalate", "learn"]:
    """Route based on symbolic governance, OPA policies, and risk tier."""
    gov = state.get("governance") or {}
    allowed = gov.get("allowed", True)
    requires_approval = gov.get("requires_human_approval", False)
    self_healed = gov.get("self_healed", False) or state.get("resolved", False)

    if self_healed:
        logger.info("[Workflow Router] Target self-healed before execution — routing directly to learn")
        return "learn"

    if not allowed:
        logger.warning("[Workflow Router] Governance BLOCKED plan — routing to escalate")
        return "escalate"

    if requires_approval:
        logger.info("[Workflow Router] Plan requires human approval — routing to approval")
        return "approval"

    logger.info("[Workflow Router] Plan approved — routing to execute")
    return "execute"



def route_after_approval(state: IncidentGraphState) -> Literal["execute", "escalate"]:
    """Route based on human operator verdict."""
    decision = state.get("approval_decision", "")
    if str(decision).lower() in ("approved", "yes", "proceed"):
        return "execute"
    return "escalate"


def route_after_execute(state: IncidentGraphState) -> Literal["verify", "rollback"]:
    """Route based on execution success or failure."""
    if state.get("fsm_state") == IncidentState.ROLLING_BACK.value or state.get("error_message"):
        return "rollback"
    return "verify"


def route_after_verify(state: IncidentGraphState) -> Literal["learn", "plan", "rollback"]:
    """
    Route based on post-check verification:
      - Resolved & healthy -> learn (success path)
      - Unhealthy & retries remaining -> plan (retry path)
      - Unhealthy & max retries reached -> rollback (failure path)
    """
    if state.get("resolved") or state.get("fsm_state") == IncidentState.RESOLVED.value:
        return "learn"

    if state.get("fsm_state") == IncidentState.RETRYING.value:
        logger.info("[Workflow Router] Target unhealthy; retrying remediation")
        return "plan"

    logger.warning("[Workflow Router] Target unhealthy and retries exhausted; rolling back")
    return "rollback"


# ── Graph Builder ─────────────────────────────────────────────────────────────

def build_incident_graph(checkpointer: Any = None) -> Any:
    """Construct and compile the unified NEXUS incident response StateGraph."""
    workflow = StateGraph(IncidentGraphState)

    # 1. Register Nodes
    workflow.add_node("triage", triage_node)
    workflow.add_node("collect", collect_telemetry_node)
    workflow.add_node("diagnose", diagnose_node)
    workflow.add_node("plan", plan_remediation_node)
    workflow.add_node("govern", govern_node)
    workflow.add_node("approval", approval_node)
    workflow.add_node("execute", execute_node)
    workflow.add_node("verify", verify_node)
    workflow.add_node("rollback", rollback_node)
    workflow.add_node("learn", learn_node)
    workflow.add_node("escalate", escalate_node)

    # 2. Linear Entry & Analysis Edges
    workflow.add_edge(START, "triage")
    workflow.add_edge("triage", "collect")
    workflow.add_edge("collect", "diagnose")
    workflow.add_edge("diagnose", "plan")
    workflow.add_edge("plan", "govern")

    # 3. Governance Conditional Branching
    workflow.add_conditional_edges(
        "govern",
        route_after_govern,
        {
            "execute": "execute",
            "approval": "approval",
            "escalate": "escalate",
            "learn": "learn",
        },
    )


    # 4. Approval Conditional Branching
    workflow.add_conditional_edges(
        "approval",
        route_after_approval,
        {
            "execute": "execute",
            "escalate": "escalate",
        },
    )

    # 5. Execution Conditional Branching
    workflow.add_conditional_edges(
        "execute",
        route_after_execute,
        {
            "verify": "verify",
            "rollback": "rollback",
        },
    )

    # 6. Verification Conditional Branching
    workflow.add_conditional_edges(
        "verify",
        route_after_verify,
        {
            "learn": "learn",
            "plan": "plan",
            "rollback": "rollback",
        },
    )

    # 7. Rollback & Terminal Edges
    workflow.add_edge("rollback", "escalate")
    workflow.add_edge("learn", END)
    workflow.add_edge("escalate", END)

    return workflow.compile(checkpointer=checkpointer)


# ── Incident Workflow Controller Class ────────────────────────────────────────

class IncidentWorkflow:
    """High-level interface for executing and managing NEXUS LangGraph incidents."""

    def __init__(
        self,
        checkpointer_store: GraphCheckpointStore | None = None,
        nats_client: Any = None,
        correlator: Any = None,
        **kwargs: Any,
    ) -> None:
        self.store = checkpointer_store or GraphCheckpointStore.from_env()
        self._compiled_graph = build_incident_graph(checkpointer=self.store.saver)
        self.nats = nats_client
        if correlator is None:
            try:
                from nexus.reasoning.event_correlator import EventCorrelator
                correlator = EventCorrelator()
            except Exception as _corr_exc:
                logger.warning("[IncidentWorkflow] Could not default EventCorrelator: %s", _corr_exc)
        self.correlator = correlator
        self._flush_interval = kwargs.get("flush_interval_s", 15.0)
        self._flush_task: asyncio.Task | None = None
        self._active_incidents: dict[str, dict[str, Any]] = {}
        self._pending_approvals: dict[str, dict[str, Any]] = {}
        self._rca_results: list[dict[str, Any]] = []
        self._running = False
        self._sub_tasks: list[asyncio.Task] = []
        self._incidents_processed = 0
        self._actions_dispatched = 0

    async def _on_event(self, event: Any) -> None:
        """Handler for single incoming IncidentEvent, dropping stale events > 120s."""
        import asyncio
        from datetime import datetime, timedelta, timezone

        from nexus.bus.incident_event import IncidentEvent

        if isinstance(event, IncidentEvent) and event.timestamp:
            age = datetime.now(timezone.utc) - event.timestamp
            if age > timedelta(seconds=120):
                logger.info(
                    "[IncidentWorkflow] Dropping stale event: %s (age=%.1fs)",
                    event.event_id,
                    age.total_seconds(),
                )
                return

        # Anti-loop: ignore events emitted by orchestrator / nexus decisions
        agent_str = str(getattr(event, "agent", "")).lower()
        if agent_str in ("orchestrator", "nexus"):
            return

        logger.info(
            "[IncidentWorkflow] Ingested event %s: agent=%s, signal=%s, resource=%s",
            getattr(event, "event_id", "unknown"),
            getattr(event, "agent", "unknown"),
            getattr(event, "signal_type", "unknown"),
            getattr(event, "resource_name", "unknown"),
        )

        if self.correlator and hasattr(self.correlator, "ingest"):
            cluster = self.correlator.ingest(event)
            if cluster:
                self._incidents_processed += 1
                task = asyncio.create_task(
                    self._process_cluster(cluster),
                    name=f"process-{getattr(cluster, 'cluster_id', 'cluster')}",
                )
                self._sub_tasks.append(task)
        else:
            self._incidents_processed += 1
            inc_id = getattr(event, "event_id", str(uuid.uuid4()))
            e_dict = (
                event.model_dump()
                if hasattr(event, "model_dump")
                else event.to_dict()
                if hasattr(event, "to_dict")
                else event
                if isinstance(event, dict)
                else {}
            )
            task = asyncio.create_task(
                self.run_incident(
                    {"incident_id": inc_id, "events": [e_dict]},
                    thread_id=inc_id,
                ),
                name=f"process-{inc_id}",
            )
            self._sub_tasks.append(task)

    async def _flush_loop(self) -> None:
        """Periodically flush stale clusters from correlator."""
        import asyncio

        while self._running:
            try:
                await asyncio.sleep(self._flush_interval)
                if self.correlator and hasattr(self.correlator, "flush_stale"):
                    stale = self.correlator.flush_stale()
                    for cluster in stale:
                        self._incidents_processed += 1
                        task = asyncio.create_task(
                            self._process_cluster(cluster),
                            name=f"flush-{getattr(cluster, 'cluster_id', 'cluster')}",
                        )
                        self._sub_tasks.append(task)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("[IncidentWorkflow] Flush loop error: %s", exc)

    async def _process_cluster(self, cluster: Any) -> None:
        """Process an IncidentCluster through LangGraph."""
        try:
            events_dicts = []
            for e in getattr(cluster, "events", []):
                if hasattr(e, "model_dump"):
                    events_dicts.append(e.model_dump())
                elif hasattr(e, "to_dict"):
                    events_dicts.append(e.to_dict())
                elif isinstance(e, dict):
                    events_dicts.append(e)
                else:
                    events_dicts.append({
                        "agent": getattr(getattr(e, "agent", None), "value", "k8s"),
                        "signal_type": getattr(getattr(e, "signal_type", None), "value", "threshold_breach"),
                        "severity": getattr(getattr(e, "severity", None), "value", "warning"),
                        "namespace": getattr(e, "namespace", "default"),
                        "resource_name": getattr(e, "resource_name", "unknown"),
                        "resource_kind": getattr(e, "resource_kind", "Deployment"),
                        "context": getattr(e, "context", {}),
                    })
            inc_id = getattr(cluster, "cluster_id", str(uuid.uuid4()))
            logger.info(
                "[IncidentWorkflow] Processing cluster %s with %d event(s) (ns=%s, resource=%s)",
                inc_id,
                len(events_dicts),
                getattr(cluster, "namespace", "default"),
                getattr(cluster, "primary_resource", "unknown"),
            )
            await self.run_incident({
                "incident_id": inc_id,
                "events": events_dicts,
                "severity": getattr(cluster, "highest_severity", "warning"),
            }, thread_id=inc_id)
        except Exception as exc:
            logger.error(
                "[IncidentWorkflow] Error processing cluster %s: %s",
                getattr(cluster, "cluster_id", "unknown"),
                exc,
                exc_info=True,
            )

    async def notify_incident_rejected(self, target_key: str, incident_id: str, reason: str = "") -> None:
        """Clear active incident tracking and mark FSM as rejected."""
        inc_data = self._active_incidents.pop(target_key, None)
        if inc_data and "fsm" in inc_data:
            fsm = inc_data["fsm"]
            from nexus.engine.fsm import IncidentState
            if hasattr(fsm, "transition_to"):
                await fsm.transition_to(IncidentState.REJECTED, reason=reason)
            elif hasattr(fsm, "_current_state"):
                fsm._current_state = IncidentState.REJECTED
        if self.nats and hasattr(self.nats, "publish_raw"):
            await self.nats.publish_raw(
                "nexus.incidents.rejected",
                {"incident_id": incident_id, "target": target_key, "reason": reason},
            )

    @property
    def status(self) -> dict[str, Any]:
        return {
            "mode": "langgraph_agent",
            "active_incidents": len(self._active_incidents),
            "pending_approvals": len(self.pending_approvals()),
            "incidents_processed": self._incidents_processed,
            "last_rca_count": len(self._rca_results),
        }

    def pending_approvals(self) -> list[dict[str, Any]]:
        """Return unique pending human approvals awaiting operator authorization."""
        seen: set[str] = set()
        approvals: list[dict[str, Any]] = []
        for item in self._pending_approvals.values():
            aid = item.get("approval_id")
            if aid and aid not in seen:
                seen.add(aid)
                approvals.append(item)
        return approvals

    def has_pending(self, action_id: str) -> bool:
        """Check if action_id, thread_id, or incident_id has a pending approval."""
        if action_id in self._pending_approvals:
            return True
        for val in self._pending_approvals.values():
            if val.get("approval_id") == action_id or val.get("incident_id") == action_id:
                return True
        return False

    def last_rca_results(self, n: int = 10) -> list[dict[str, Any]]:
        return self._rca_results[-n:]

    async def start(self, nats_client: Any = None) -> None:
        """Start listening on NATS incident subjects and processing workflows."""
        import asyncio

        if nats_client:
            self.nats = nats_client
        self._running = True
        logger.info("[IncidentWorkflow] Started LangGraph production incident listener")

        if not self.correlator:
            try:
                from nexus.reasoning.event_correlator import EventCorrelator
                self.correlator = EventCorrelator()
            except Exception as e_corr:
                logger.warning("[IncidentWorkflow] Could not instantiate EventCorrelator: %s", e_corr)

        self._flush_task = asyncio.create_task(self._flush_loop(), name="workflow-flush")
        self._sub_tasks.append(self._flush_task)

        if self.nats:
            if hasattr(self.nats, "subscribe"):
                try:
                    await self.nats.subscribe(
                        handler=self._on_event,
                        agent_filter=">",
                    )
                    logger.info("[IncidentWorkflow] Subscribed to NATS nexus.incidents.>")
                except Exception as nats_sub_exc:
                    logger.warning("[IncidentWorkflow] NATS subscribe failed: %s", nats_sub_exc)

            if hasattr(self.nats, "subscribe_raw"):
                async def _raw_handler(data: dict[str, Any], subject: str = "") -> None:
                    inc_id = data.get("incident_id") or data.get("event_id") or str(uuid.uuid4())
                    logger.info("[IncidentWorkflow] Ingested raw incident signal from %s: %s", subject, inc_id)
                    self._incidents_processed += 1
                    t = asyncio.create_task(self.run_incident(data, thread_id=inc_id))
                    self._sub_tasks.append(t)

                try:
                    await self.nats.subscribe_raw("ppa.incidents.>", handler=_raw_handler)
                except Exception as raw_exc:
                    logger.debug("[IncidentWorkflow] NATS raw subscription failed (non-fatal): %s", raw_exc)

    async def stop(self) -> None:
        """Stop background tasks."""
        import asyncio

        self._running = False
        for t in self._sub_tasks:
            t.cancel()
        if self._sub_tasks:
            await asyncio.gather(*self._sub_tasks, return_exceptions=True)
        self._sub_tasks = []

    async def run_incident(
        self,
        incident_data: dict[str, Any],
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Run incident response workflow from initial event signals.

        Args:
            incident_data: Dictionary containing incident_id, events, and optional overrides.
            thread_id: Unique thread identifier for state persistence and interruption.
        """
        inc_id = incident_data.get("incident_id") or str(uuid.uuid4())
        tid = thread_id or inc_id

        events = incident_data.get("events", [])
        if not events and "event" in incident_data:
            events = [incident_data["event"]]

        initial_state: IncidentGraphState = {
            "incident_id": inc_id,
            "fsm_state": IncidentState.DETECTED.value,
            "events": events,
            "platform": incident_data.get("platform", "kubernetes"),
            "target": incident_data.get("target", {}),
            "severity": incident_data.get("severity", "warning"),
            "telemetry": {},
            "diagnosis": {},
            "diagnostic_reflections": [],
            "plan": None,
            "remediation_history": [],
            "governance": None,
            "approval_id": None,
            "approval_decision": incident_data.get("approval_decision"),
            "execution_records": [],
            "rollback_executed": False,
            "rollback_records": [],
            "verification": None,
            "retry_count": 0,
            "max_retries": incident_data.get("max_retries", 2),
            "audit_log": [],
            "messages": [],
            "error_message": None,
            "resolved": False,
            "escalated": False,
        }

        config = {"configurable": {"thread_id": tid}}
        logger.info("[IncidentWorkflow] Starting workflow for incident %s (thread=%s)", inc_id, tid)

        result = await self._compiled_graph.ainvoke(initial_state, config=config)
        if result.get("diagnosis"):
            self._rca_results.append(result["diagnosis"])
            self._rca_results = self._rca_results[-100:]

        # Check if the execution paused for human approval (interrupt)
        state_snapshot = self._compiled_graph.get_state(config)
        if (state_snapshot.next and "approval" in state_snapshot.next) or result.get("__interrupt__"):
            await self._record_pending_approval(result, inc_id=inc_id, thread_id=tid)

        return result

    async def _record_pending_approval(
        self,
        state_or_result: dict[str, Any],
        inc_id: str,
        thread_id: str,
    ) -> dict[str, Any]:
        """Format and record pending approval entry and broadcast via NATS."""
        from datetime import datetime, timezone

        plan = state_or_result.get("plan") or {}
        if hasattr(plan, "model_dump"):
            plan_dict = plan.model_dump()
        elif isinstance(plan, dict):
            plan_dict = plan
        else:
            plan_dict = {}

        steps = plan_dict.get("steps", [])
        action_type = steps[0].get("tool_name", "k8s_remediation") if steps else "k8s_remediation"
        runbook_id = plan_dict.get("failure_mode") or "autonomous_remediation"
        confidence = float(state_or_result.get("diagnosis", {}).get("confidence", 0.8))

        target = state_or_result.get("target") or {}
        if hasattr(target, "model_dump"):
            target_dict = target.model_dump()
        elif isinstance(target, dict):
            target_dict = target
        else:
            target_dict = {}

        target_ns = target_dict.get("namespace", "default")
        target_name = target_dict.get("name", "unknown")
        target_str = f"{target_ns}/{target_name}"

        gov = state_or_result.get("governance") or {}
        if hasattr(gov, "model_dump"):
            gov_dict = gov.model_dump()
        elif isinstance(gov, dict):
            gov_dict = gov
        else:
            gov_dict = {}
        reasons = gov_dict.get("reasons", ["L2/L3 mutating action requires operator authorization"])

        diagnosis = state_or_result.get("diagnosis") or {}
        if hasattr(diagnosis, "model_dump"):
            diag_dict = diagnosis.model_dump()
        elif hasattr(diagnosis, "to_dict"):
            diag_dict = diagnosis.to_dict()
        elif isinstance(diagnosis, dict):
            diag_dict = diagnosis
        else:
            diag_dict = {}

        suggested_fix = (
            diag_dict.get("suggested_fix")
            or (steps[0].get("description") if steps else None)
            or f"Execute {action_type} on {target_str}"
        )

        approval_id = thread_id
        pending_entry = {
            "approval_id": approval_id,
            "thread_id": thread_id,
            "incident_id": inc_id,
            "runbook_id": runbook_id,
            "action_type": action_type,
            "target": target_str,
            "healing_level": 3,
            "confidence": round(confidence, 3),
            "enqueued_at": datetime.now(timezone.utc).isoformat(),
            "reasons": reasons,
            "prompt": suggested_fix,
            "suggested_fix": suggested_fix,
            "rca": diag_dict,
            "plan": plan_dict,
            "context": {
                "namespace": target_ns,
                "platform": state_or_result.get("platform", "kubernetes"),
                "thread_id": thread_id,
                "target": target_dict,
                "rca": diag_dict,
                "suggested_fix": suggested_fix,
            },
        }

        self._pending_approvals[approval_id] = pending_entry
        self._pending_approvals[inc_id] = pending_entry
        self._pending_approvals[thread_id] = pending_entry

        logger.info(
            "[IncidentWorkflow] Staged pending approval %s for incident %s (%s)",
            approval_id,
            inc_id,
            target_str,
        )

        if self.nats and hasattr(self.nats, "publish_raw"):
            try:
                await self.nats.publish_raw(
                    "nexus.approvals.required",
                    {
                        "approval_id": approval_id,
                        "runbook_id": runbook_id,
                        "action_type": action_type,
                        "target": target_str,
                        "incident_id": inc_id,
                        "healing_level": 3,
                        "confidence": confidence,
                        "app": target_ns,
                        "context": pending_entry["context"],
                    },
                )
                logger.info("[IncidentWorkflow] Published nexus.approvals.required for %s", approval_id)
            except Exception as e_pub:
                logger.warning("[IncidentWorkflow] Failed publishing nexus.approvals.required: %s", e_pub)

        return pending_entry

    async def resume_incident(
        self,
        thread_id: str,
        approval_decision: str = "approved",
    ) -> dict[str, Any]:
        """Resume an interrupted incident after human authorization."""
        from langgraph.types import Command

        # Resolve thread_id if an approval_id or incident_id was provided
        entry = self._pending_approvals.get(thread_id)
        if not entry:
            for val in list(self._pending_approvals.values()):
                if val.get("approval_id") == thread_id or val.get("incident_id") == thread_id:
                    entry = val
                    break

        actual_tid = entry.get("thread_id", thread_id) if entry else thread_id

        # Clean up from pending approvals
        if entry:
            for k in [entry.get("approval_id"), entry.get("thread_id"), entry.get("incident_id")]:
                if k and k in self._pending_approvals:
                    self._pending_approvals.pop(k, None)
        else:
            self._pending_approvals.pop(thread_id, None)

        config = {"configurable": {"thread_id": actual_tid}}
        logger.info(
            "[IncidentWorkflow] Resuming incident on thread %s (input=%s) with decision '%s'",
            actual_tid,
            thread_id,
            approval_decision,
        )

        result = await self._compiled_graph.ainvoke(
            Command(resume=approval_decision),
            config=config,
        )

        # If another interruption occurred, stage again
        state_snapshot = self._compiled_graph.get_state(config)
        if (state_snapshot.next and "approval" in state_snapshot.next) or result.get("__interrupt__"):
            inc_id = result.get("incident_id", actual_tid)
            await self._record_pending_approval(result, inc_id=inc_id, thread_id=actual_tid)
        else:
            if approval_decision.lower() in ("rejected", "no", "false"):
                target_obj = result.get("target") or (entry.get("context", {}).get("target") if entry else {})
                target_str = target_obj.get("name", "unknown") if isinstance(target_obj, dict) else str(target_obj)
                await self.notify_incident_rejected(
                    target_str,
                    result.get("incident_id", actual_tid),
                    reason=f"Rejected by operator: {approval_decision}",
                )

        if result.get("diagnosis"):
            self._rca_results.append(result["diagnosis"])
            self._rca_results = self._rca_results[-100:]
        return result
