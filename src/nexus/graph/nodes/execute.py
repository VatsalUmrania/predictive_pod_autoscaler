"""
NEXUS Governed Execution Node
=============================
Executes the approved remediation plan step-by-step through the platform adapter.
Captures a pre-mutation snapshot before any state change and carries its inverse
(rollback_tool/parameters) in the execution record, guaranteeing deterministic
undo capability without LLM improvisation.
"""

from __future__ import annotations

import logging
import time
import zlib
from datetime import datetime, timezone
from typing import Any

from nexus.engine.fsm import IncidentState
from nexus.graph.platform import TargetResource, get_platform_registry
from nexus.graph.state import ExecutionRecord, IncidentGraphState
from nexus.db import get_database_client
from nexus.tools.base import NexusToolResult
from nexus.tools.registry import default_registry

logger = logging.getLogger(__name__)


async def execute_node(state: IncidentGraphState) -> dict[str, Any]:
    """Execute remediation steps through the platform adapter with rollback snapshots."""
    incident_id = state["incident_id"]
    plan = state.get("plan") or {}
    steps = plan.get("steps", [])
    target_dict = state.get("target") or {}
    target = (
        TargetResource(**target_dict) if isinstance(target_dict, dict) else target_dict
    )
    platform_id = state.get("platform", "kubernetes")
    target_service = (
        target.name
        if hasattr(target, "name")
        else str(target_dict.get("name", "unknown"))
    )

    registry = get_platform_registry()
    adapter = registry.get(platform_id) or registry.resolve_adapter(
        state.get("events", [])
    )

    records: list[dict[str, Any]] = []
    overall_success = True
    error_msg = None

    logger.info(
        "[Execute] Executing %d remediation step(s) for incident %s on %s",
        len(steps),
        incident_id,
        target,
    )

    # 1. PostgreSQL Advisory Locking (guard against concurrent mutation execution)
    conn = None
    conn_ctx = None
    lock_acquired = False
    lock_key = zlib.crc32(target_service.encode())

    try:
        db_client = await get_database_client()
        conn_ctx = db_client.acquire()
        conn = await conn_ctx.__aenter__()
        if hasattr(conn, "fetchval"):
            lock_res = await conn.fetchval("SELECT pg_try_advisory_lock($1)", lock_key)
            lock_acquired = bool(lock_res)
            if not lock_acquired:
                logger.warning(
                    "[Execute] Could not acquire advisory lock for service %s (key=%d); concurrent action in progress",
                    target_service,
                    lock_key,
                )
                now_iso = datetime.now(timezone.utc).isoformat()
                return {
                    "execution_records": [],
                    "fsm_state": IncidentState.FAILED.value,
                    "error_message": f"Could not acquire execution lock for service '{target_service}' — concurrent mutation in progress",
                    "audit_log": [
                        {
                            "timestamp": now_iso,
                            "stage": "execute",
                            "action": "lock_contention",
                            "details": f"Blocked by advisory lock on {target_service}",
                        }
                    ],
                }
    except Exception as lock_err:
        logger.debug("[Execute] Advisory locking skipped/unsupported: %s", lock_err)

    try:
        for step in steps:
            tool_name = step.get("tool_name", "")
            params = step.get("parameters", {})
            step_idx = step.get("step_index", 0)

            # 1. Capture Pre-Mutation Snapshot (carried in the execution record for rollback)
            snapshot = None
            snapshot_id = None
            if adapter is not None and hasattr(adapter, "capture_snapshot"):
                try:
                    snapshot = await adapter.capture_snapshot(target, tool_name, params)
                    if snapshot:
                        snapshot_id = snapshot.snapshot_id
                except Exception as snap_exc:
                    logger.warning(
                        "[Execute] Failed to capture pre-state snapshot: %s", snap_exc
                    )

            # 2. Execute Action via Platform Adapter or default ToolRegistry
            t0 = time.monotonic()
            res = None
            if adapter is not None and hasattr(adapter, "execute_action"):
                res = await adapter.execute_action(tool_name, params)
                if not res.success and "not registered in" in str(res.error or ""):
                    reg_tool = default_registry.get(tool_name)
                    if reg_tool:
                        res = await default_registry.execute_tool(tool_name, params)
            if res is None:
                reg_tool = default_registry.get(tool_name)
                if reg_tool:
                    res = await default_registry.execute_tool(tool_name, params)
                else:
                    res = NexusToolResult(
                        success=False,
                        error=f"Tool '{tool_name}' not registered in adapter or default registry",
                    )
            duration_ms = (time.monotonic() - t0) * 1000.0

            rec = ExecutionRecord(
                step_index=step_idx,
                tool_name=tool_name,
                success=res.success,
                result_data=res.data,
                error=res.error,
                snapshot_id=snapshot_id,
                duration_ms=duration_ms,
                rollback_tool=snapshot.rollback_tool if snapshot else None,
                rollback_parameters=(
                    dict(snapshot.rollback_parameters) if snapshot else None
                ),
            )
            records.append(rec.model_dump())

            if not res.success:
                logger.error(
                    "[Execute] Step %d (%s) FAILED: %s", step_idx, tool_name, res.error
                )
                overall_success = False
                error_msg = f"Step {step_idx} ({tool_name}) failed: {res.error}"
                break

        now_iso = datetime.now(timezone.utc).isoformat()
        audit_entry = {
            "timestamp": now_iso,
            "stage": "execute",
            "action": "steps_executed",
            "details": f"Success: {overall_success} | Steps: {len(records)} | Error: {error_msg or 'None'}",
        }

        next_fsm = (
            IncidentState.VERIFYING.value
            if overall_success
            else IncidentState.ROLLING_BACK.value
        )

        return {
            "execution_records": records,
            "fsm_state": next_fsm,
            "error_message": error_msg,
            "audit_log": [audit_entry],
        }

    finally:
        if conn is not None and lock_acquired:
            try:
                await conn.execute("SELECT pg_advisory_unlock($1)", lock_key)
                logger.debug("[Execute] Released advisory lock for key %d", lock_key)
            except Exception as unlock_err:
                logger.warning(
                    "[Execute] Failed to release advisory lock: %s", unlock_err
                )
        if conn_ctx is not None:
            try:
                await conn_ctx.__aexit__(None, None, None)
            except Exception:
                pass
