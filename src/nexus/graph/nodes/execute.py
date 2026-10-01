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
from datetime import datetime, timezone
from typing import Any

from nexus.engine.fsm import IncidentState
from nexus.graph.platform import TargetResource, get_platform_registry
from nexus.graph.state import ExecutionRecord, IncidentGraphState

logger = logging.getLogger(__name__)


async def execute_node(state: IncidentGraphState) -> dict[str, Any]:
    """Execute remediation steps through the platform adapter with rollback snapshots."""
    incident_id = state["incident_id"]
    plan = state.get("plan") or {}
    steps = plan.get("steps", [])
    target_dict = state.get("target") or {}
    target = TargetResource(**target_dict) if isinstance(target_dict, dict) else target_dict
    platform_id = state.get("platform", "kubernetes")

    registry = get_platform_registry()
    adapter = registry.get(platform_id) or registry.resolve_adapter(state.get("events", []))

    records: list[dict[str, Any]] = []
    overall_success = True
    error_msg = None

    logger.info(
        "[Execute] Executing %d remediation step(s) for incident %s on %s",
        len(steps),
        incident_id,
        target,
    )

    for step in steps:
        tool_name = step.get("tool_name", "")
        params = step.get("parameters", {})
        step_idx = step.get("step_index", 0)

        # 1. Capture Pre-Mutation Snapshot (carried in the execution record for rollback)
        snapshot = None
        snapshot_id = None
        try:
            snapshot = await adapter.capture_snapshot(target, tool_name, params)
            if snapshot:
                snapshot_id = snapshot.snapshot_id
        except Exception as snap_exc:
            logger.warning("[Execute] Failed to capture pre-state snapshot: %s", snap_exc)

        # 2. Execute Action via Platform Adapter
        t0 = time.monotonic()
        res = await adapter.execute_action(tool_name, params)
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
            rollback_parameters=dict(snapshot.rollback_parameters) if snapshot else None,
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
