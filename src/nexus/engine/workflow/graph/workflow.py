"""
NEXUS Interrupt-Aware LangGraph Incident Workflow
=================================================
Provides the standalone `run_incident_workflow` entrypoint and the high-level
`IncidentWorkflow` orchestration class bridged cleanly to `run_incident_workflow`,
`handle_incident_approval`, and `finalize_workflow_run`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, cast

from nexus.engine.workflow.graph.builder import build_incident_graph
from nexus.engine.workflow.graph.checkpointer import GraphCheckpointStore
from nexus.engine.workflow.graph.routes import (
    route_after_approval,
    route_after_diagnose,
    route_after_execute,
    route_after_govern,
    route_after_plan,
    route_after_triage,
    route_after_verify,
)
from nexus.engine.workflow.lifecycle.workflow_approval import (
    create_pending_approval_entry,
    handle_incident_approval,
    notify_incident_rejected_helper,
    publish_pending_approval,
    resume_incident_execution,
)
from nexus.engine.workflow.lifecycle.workflow_completion import (
    finalize_workflow_run,
    handle_workflow_completion,
)
from nexus.engine.workflow.lifecycle.workflow_events import (
    build_cluster_event_dicts,
    build_initial_state,
    handle_incoming_event,
    run_flush_loop,
    setup_event_subscriptions,
)
from nexus.engine.workflow.state.workflow_state import (
    ensure_incident_record,
    sync_discovered_target_identity,
)
from nexus.services.notifications.dispatcher.notifier import (
    send_approval_request_notification,
)

logger = logging.getLogger(__name__)

__all__ = [
    "run_incident_workflow",
    "handle_incident_approval",
    "finalize_workflow_run",
    "IncidentWorkflow",
    "build_incident_graph",
    "route_after_triage",
    "route_after_diagnose",
    "route_after_plan",
    "route_after_govern",
    "route_after_approval",
    "route_after_execute",
    "route_after_verify",
]


async def run_incident_workflow(
    compiled_graph: Any,
    initial_state: Mapping[str, Any],
    *,
    notify_on_pause: bool = True,
) -> dict[str, Any]:
    incident_id = initial_state["incident_id"]
    config = {"configurable": {"thread_id": incident_id}}

    final_state = await compiled_graph.ainvoke(initial_state, config=config)
    snapshot = await compiled_graph.aget_state(config)

    # If snapshot.next is non-empty, the graph paused at `approval_node` via interrupt()
    if snapshot and snapshot.next:
        logger.info(
            "Incident %s paused at interrupt node(s) %s; notify_on_pause=%s.",
            incident_id,
            snapshot.next,
            notify_on_pause,
        )
        await sync_discovered_target_identity(incident_id, snapshot.values)
        if notify_on_pause:
            await send_approval_request_notification(incident_id, snapshot.values)
        return cast(dict[str, Any], snapshot.values)

    # Graph reached a terminal state (END)
    await sync_discovered_target_identity(incident_id, final_state)
    await finalize_workflow_run(incident_id, final_state)
    return cast(dict[str, Any], final_state)


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
                from nexus.engine.reasoning.event_correlator import EventCorrelator

                correlator = EventCorrelator()
            except Exception:
                pass
        self.correlator = correlator
        self._flush_interval = kwargs.get("flush_interval_s", 15.0)
        self._flush_task: asyncio.Task[Any] | None = None
        self._active_incidents: dict[str, dict[str, Any]] = {}
        self._pending_approvals: dict[str, dict[str, Any]] = {}
        self._rca_results: list[dict[str, Any]] = []
        self._running, self._incidents_processed, self._actions_dispatched = False, 0, 0
        self._sub_tasks: list[asyncio.Task[Any]] = []

    async def _on_event(self, event: Any) -> None:
        await handle_incoming_event(self, event)

    async def _flush_loop(self) -> None:
        await run_flush_loop(self)

    async def _process_cluster(self, cluster: Any) -> None:
        try:
            events = build_cluster_event_dicts(cluster)
            inc_id = getattr(cluster, "cluster_id", str(uuid.uuid4()))
            sev = getattr(cluster, "highest_severity", "warning")
            await self.run_incident(
                {"incident_id": inc_id, "raw_events": events, "severity": sev},
                thread_id=inc_id,
            )
        except Exception as exc:
            cid = getattr(cluster, "cluster_id", "unknown")
            logger.error(
                "[IncidentWorkflow] Error processing cluster %s: %s",
                cid,
                exc,
                exc_info=True,
            )

    async def notify_incident_rejected(
        self, target_key: str, incident_id: str, reason: str = ""
    ) -> None:
        await notify_incident_rejected_helper(
            self._active_incidents, self.nats, target_key, incident_id, reason
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
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for v in self._pending_approvals.values():
            aid = v.get("approval_id")
            if aid and aid not in seen:
                seen.add(aid)
                out.append(v)
        return out

    def has_pending(self, action_id: str) -> bool:
        return action_id in self._pending_approvals or any(
            val.get("approval_id") == action_id or val.get("incident_id") == action_id
            for val in self._pending_approvals.values()
        )

    def last_rca_results(self, n: int = 10) -> list[dict[str, Any]]:
        return self._rca_results[-n:]

    async def start(self, nats_client: Any = None) -> None:
        if nats_client:
            self.nats = nats_client
        self._running = True
        logger.info("[IncidentWorkflow] Started LangGraph production incident listener")
        self._flush_task = asyncio.create_task(
            self._flush_loop(), name="workflow-flush"
        )
        self._sub_tasks.append(self._flush_task)
        await setup_event_subscriptions(self)

    async def stop(self) -> None:
        self._running = False
        for t in self._sub_tasks:
            t.cancel()
        if self._sub_tasks:
            await asyncio.gather(*self._sub_tasks, return_exceptions=True)
        self._sub_tasks.clear()

    async def run_incident(
        self, incident_data: dict[str, Any], thread_id: str | None = None
    ) -> dict[str, Any]:
        inc_id = incident_data.get("incident_id") or str(uuid.uuid4())
        tid = thread_id or inc_id
        initial_state = build_initial_state(incident_data, inc_id)
        initial_state["incident_id"] = tid

        logger.info(
            "[IncidentWorkflow] Starting workflow for incident %s (thread=%s)",
            inc_id,
            tid,
        )

        # agent_runs.incident_id is a foreign key into `incidents`, so the parent
        # row must exist before the reasoning nodes can record their runs.
        await ensure_incident_record(inc_id, incident_data)

        # When NATS is configured, _record_pending_approval -> publish_pending_approval
        # dispatches the single canonical Slack approval card via Notifier.
        has_nats_publisher = bool(self.nats and hasattr(self.nats, "publish_raw"))
        result = await run_incident_workflow(
            self._compiled_graph,
            initial_state,
            notify_on_pause=not has_nats_publisher,
        )
        if result.get("rca_result"):
            rca = dict(result["rca_result"])
            # The dashboard's RCA feed shows when each analysis was produced.
            rca.setdefault("decided_at", datetime.now(timezone.utc).isoformat())
            self._rca_results.append(rca)
            self._rca_results = self._rca_results[-100:]

        config = {"configurable": {"thread_id": tid}}
        snapshot = await self._compiled_graph.aget_state(config)
        if snapshot and snapshot.next:
            await self._record_pending_approval(result, inc_id=inc_id, thread_id=tid)
        else:
            await self._handle_workflow_completion(
                result, inc_id=inc_id, thread_id=tid, publish_report=False
            )

        return cast(dict[str, Any], result)

    async def _handle_workflow_completion(
        self,
        result: dict[str, Any],
        inc_id: str,
        thread_id: str,
        *,
        publish_report: bool = True,
    ) -> None:
        await handle_workflow_completion(
            result, inc_id, thread_id, self.nats, publish_report=publish_report
        )

    async def _record_pending_approval(
        self, state_or_result: dict[str, Any], inc_id: str, thread_id: str
    ) -> dict[str, Any]:
        entry = create_pending_approval_entry(state_or_result, inc_id, thread_id)
        for k in (entry["approval_id"], inc_id, thread_id):
            self._pending_approvals[k] = entry
        logger.info(
            "[IncidentWorkflow] Staged pending approval %s for incident %s (target=%s)",
            entry["approval_id"],
            inc_id,
            entry["target"],
        )
        await publish_pending_approval(self.nats, entry)
        return entry

    async def resume_incident(
        self,
        thread_id: str,
        approval_decision: str | dict[str, Any] = "approved",
    ) -> dict[str, Any]:
        return await resume_incident_execution(self, thread_id, approval_decision)
