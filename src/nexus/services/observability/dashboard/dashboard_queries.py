"""
Aggregated incident queries and claim management for the developer dashboard.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from nexus.services.observability.dashboard.dashboard_incidents import (
    serialize_incident_for_dashboard,
)

logger = logging.getLogger(__name__)


def _matches_app_filter(item: Mapping[str, Any], app: str | None) -> bool:
    if not app:
        return True
    needle = app.lower()
    candidates = (
        item.get("target_resource"),
        item.get("target"),
        item.get("service_name"),
        item.get("namespace"),
    )
    return any(needle in str(val).lower() for val in candidates if val)


def _state_from_pending_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Construct fallback IncidentState fields from a workflow pending_approvals entry."""
    ctx_raw = entry.get("context")
    ctx_map: dict[str, Any] = dict(ctx_raw) if isinstance(ctx_raw, Mapping) else {}
    reasons = entry.get("reasons") or []
    reason_text = (
        reasons[0]
        if reasons
        else "Mutating remediation requires operator authorization"
    )
    return {
        "incident_id": entry.get("incident_id") or entry.get("thread_id"),
        "platform": ctx_map.get("platform"),
        "namespace": ctx_map.get("namespace"),
        "resource_kind": ctx_map.get("resource_kind"),
        "service_name": ctx_map.get("service_name"),
        "target_resource": ctx_map.get("target_resource") or entry.get("target"),
        "failure_class": ctx_map.get("failure_class") or entry.get("runbook_id"),
        "suspected_cause": ctx_map.get("suspected_cause")
        or entry.get("suggested_fix")
        or "",
        "confidence": entry.get("confidence", 0.0),
        "rca_result": entry.get("rca") or ctx_map.get("rca") or {},
        "remediation_plan": entry.get("plan") or ctx_map.get("plan") or {},
        "policy_decision": {
            "allowed": True,
            "requires_approval": True,
            "reason": reason_text,
        },
        "requires_approval": True,
        "approval_status": "PENDING",
        "status": "AWAITING_APPROVAL",
        "updated_at": entry.get("enqueued_at"),
    }


async def _load_checkpoint_snapshot(
    compiled_graph: Any, thread_id: str
) -> tuple[dict[str, Any], list[str], str | None]:
    """Safely inspect LangGraph checkpointer state for one thread_id."""
    if compiled_graph is None or not hasattr(compiled_graph, "aget_state"):
        return {}, [], None
    try:
        snapshot = await compiled_graph.aget_state(
            {"configurable": {"thread_id": thread_id}}
        )
        if not snapshot or not getattr(snapshot, "values", None):
            return {}, [], None
        values = dict(snapshot.values)
        next_nodes = [str(n) for n in (getattr(snapshot, "next", None) or ())]
        created_at = getattr(snapshot, "created_at", None)
        return values, next_nodes, str(created_at) if created_at else None
    except Exception as exc:
        logger.debug("[Dashboard] Checkpoint lookup skipped for %s: %s", thread_id, exc)
        return {}, [], None


async def fetch_developer_incidents(
    n: int,
    app: str | None,
    ctx: Any,
    read_incidents_fn: Any,
    in_memory_incidents: list[dict[str, Any]],
    make_plain_english_fn: Any,
) -> list[dict[str, Any]]:
    """Fetch, merge live LangGraph checkpoints with DB records, and serialize incidents."""
    # Map incident_id -> {"legacy": dict, "fallback_state": dict, "thread_id": str}
    merged_records: dict[str, dict[str, Any]] = {}
    anonymous_records: list[dict[str, Any]] = []

    def _upsert(
        inc_id: str | None,
        *,
        legacy: Mapping[str, Any] | None = None,
        fallback_state: Mapping[str, Any] | None = None,
        thread_id: str | None = None,
        prefer_existing_legacy: bool = False,
    ) -> None:
        if not inc_id:
            if legacy:
                anonymous_records.append(dict(legacy))
            return
        bucket = merged_records.setdefault(
            inc_id,
            {"legacy": {}, "fallback_state": {}, "thread_id": inc_id},
        )
        if thread_id:
            bucket["thread_id"] = thread_id
        if legacy:
            if prefer_existing_legacy:
                for k, v in legacy.items():
                    if k not in bucket["legacy"] or bucket["legacy"][k] is None:
                        bucket["legacy"][k] = v
            else:
                bucket["legacy"].update(
                    {k: v for k, v in legacy.items() if v is not None}
                )
        if fallback_state:
            bucket["fallback_state"].update(
                {k: v for k, v in fallback_state.items() if v is not None}
            )

    # 1. PostgreSQL developer_incidents table
    raw_results = await read_incidents_fn(n=max(n, 50), app=app)
    for row in raw_results if isinstance(raw_results, (list, tuple)) else []:
        if isinstance(row, Mapping):
            _upsert(row.get("incident_id"), legacy=row)

    # 2. In-memory developer_incidents buffer
    for entry in reversed(in_memory_incidents):
        if isinstance(entry, Mapping):
            _upsert(
                entry.get("incident_id"),
                legacy=entry,
                prefer_existing_legacy=True,
            )

    # 3. PostgreSQL incidents table (IncidentRepository)
    db_client = getattr(ctx, "db_client", None)
    if db_client is not None and hasattr(db_client, "list_incidents"):
        try:
            db_incidents = await db_client.list_incidents(limit=max(n, 50))
            for row in db_incidents or []:
                if isinstance(row, Mapping):
                    _upsert(
                        row.get("incident_id"),
                        legacy=row,
                        prefer_existing_legacy=True,
                    )
        except Exception as _e:
            logger.debug(f"[Dashboard] IncidentRepository list skipped: {_e}")

    # 4. Active & paused LangGraph workflow threads (interrupt() / pending_approvals)
    workflow = getattr(ctx, "workflow", None)
    if workflow is not None:
        if hasattr(workflow, "pending_approvals"):
            try:
                for pending in workflow.pending_approvals() or []:
                    if not isinstance(pending, Mapping):
                        continue
                    inc_id = pending.get("incident_id") or pending.get("thread_id")
                    tid = pending.get("thread_id") or inc_id
                    _upsert(
                        inc_id,
                        fallback_state=_state_from_pending_entry(pending),
                        thread_id=tid,
                    )
            except Exception as _e:
                logger.debug(
                    f"[Dashboard] Workflow pending_approvals read skipped: {_e}"
                )

        active_map = getattr(workflow, "_active_incidents", None)
        if isinstance(active_map, Mapping):
            for active_val in active_map.values():
                if isinstance(active_val, Mapping):
                    inc_id = active_val.get("incident_id") or active_val.get(
                        "thread_id"
                    )
                    if inc_id:
                        _upsert(
                            str(inc_id),
                            fallback_state=active_val,
                            thread_id=str(active_val.get("thread_id") or inc_id),
                        )

        # Discover any threads persisted in an in-memory checkpointer (e.g. MemorySaver)
        saver = getattr(getattr(workflow, "store", None), "saver", None)
        storage = getattr(saver, "storage", None)
        if isinstance(storage, Mapping):
            for thread_key in list(storage.keys()):
                if thread_key:
                    tid = str(thread_key)
                    if tid not in merged_records:
                        _upsert(tid, thread_id=tid)

    # 5. AuditTrail fallback
    if getattr(ctx, "audit_trail", None) is not None and len(merged_records) < n:
        try:
            rows = await ctx.audit_trail.query_recent(limit=max(n * 3, 60))
            for row in rows or []:
                if not isinstance(row, Mapping):
                    continue
                inc_id = row.get("incident_id") or row.get("correlation_id")
                if inc_id and inc_id in merged_records:
                    continue
                audit_legacy = {
                    "timestamp": (row.get("timestamp") or "")[:19],
                    "runbook_id": row.get("runbook_id"),
                    "target": row.get("target") or row.get("resource_name"),
                    "level": row.get("healing_level"),
                    "outcome": row.get("execution_outcome", "pending"),
                    "description": make_plain_english_fn(row),
                    "incident_id": inc_id,
                }
                _upsert(inc_id, legacy=audit_legacy, prefer_existing_legacy=True)
        except Exception as _e:
            logger.debug(f"[Dashboard] AuditTrail read skipped: {_e}")

    # 6. Legacy orchestrator RCA enrichment
    orchestrator = getattr(ctx, "orchestrator", None)
    if orchestrator is not None and hasattr(orchestrator, "last_rca_results"):
        try:
            recent_rca = orchestrator.last_rca_results(n=50)
            rca_by_inc = {
                r.get("incident_id"): r.get("rca")
                for r in recent_rca
                if isinstance(r, Mapping) and r.get("incident_id") and r.get("rca")
            }
            rca_by_target = {
                r.get("validation", {}).get("target", ""): r.get("rca")
                for r in recent_rca
                if isinstance(r, Mapping)
                and isinstance(r.get("validation"), Mapping)
                and r.get("validation", {}).get("target")
                and r.get("rca")
            }
            for inc_id, bucket in merged_records.items():
                legacy = bucket["legacy"]
                if not legacy.get("rca"):
                    target = legacy.get("target")
                    if inc_id in rca_by_inc:
                        legacy["rca"] = rca_by_inc[inc_id]
                    elif target and target in rca_by_target:
                        legacy["rca"] = rca_by_target[target]
        except Exception as _e:
            logger.debug(f"[Dashboard] RCA enrichment skipped: {_e}")

    # 7. Merge live LangGraph checkpointer snapshots and serialize
    compiled_graph = (
        getattr(workflow, "_compiled_graph", None) if workflow is not None else None
    )
    serialized: list[dict[str, Any]] = []

    for inc_id, bucket in merged_records.items():
        thread_id = str(bucket.get("thread_id") or inc_id)
        cp_state, next_nodes, cp_updated = await _load_checkpoint_snapshot(
            compiled_graph, thread_id
        )
        if not cp_state and thread_id != inc_id:
            cp_state, next_nodes, cp_updated = await _load_checkpoint_snapshot(
                compiled_graph, inc_id
            )

        combined_state: dict[str, Any] = {
            **bucket["fallback_state"],
            **cp_state,
        }
        item = serialize_incident_for_dashboard(
            incident_id=inc_id,
            state=combined_state,
            next_nodes=next_nodes,
            updated_at=cp_updated or combined_state.get("updated_at"),
            legacy_record=bucket["legacy"],
        )
        if _matches_app_filter(item, app):
            serialized.append(item)

    for idx, anon in enumerate(anonymous_records):
        item = serialize_incident_for_dashboard(
            incident_id=str(anon.get("incident_id") or f"INC-ANON-{idx}"),
            state={},
            legacy_record=anon,
        )
        if _matches_app_filter(item, app):
            serialized.append(item)

    serialized.sort(
        key=lambda r: str(r.get("updated_at") or r.get("timestamp") or ""),
        reverse=True,
    )
    return serialized[:n]


async def accept_incident_claim(
    incident_id: str,
    payload: dict[str, Any] | None,
    in_memory_incidents: list[dict[str, Any]],
    get_db_fn: Any,
) -> dict[str, Any]:
    """Claim ownership of an incident from the developer dashboard."""
    user = (payload or {}).get("user", "dev-oncall")
    now_ts = datetime.now(timezone.utc).isoformat()[:19].replace("T", " ")

    for item in in_memory_incidents:
        if item.get("incident_id") == incident_id:
            item["accepted_by"] = user
            item["accepted_at"] = now_ts

    try:
        db = await get_db_fn()
        sql = """
        UPDATE developer_incidents
        SET accepted_by = $1, accepted_at = $2
        WHERE incident_id = $3
        """
        async with db.acquire() as conn:
            await conn.execute(sql, user, now_ts, incident_id)
        logger.info(f"[Dashboard] Incident {incident_id} accepted by {user}")
    except Exception as _e:
        logger.debug(f"[Dashboard] accept DB update skipped: {_e}")

    return {
        "status": "ok",
        "incident_id": incident_id,
        "accepted_by": user,
        "accepted_at": now_ts,
    }
