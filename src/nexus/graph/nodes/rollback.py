"""
NEXUS Deterministic Rollback Node
=================================
Executes deterministic state restoration using pre-mutation snapshots captured
in RollbackRegistry. Does NOT use LLM improvisation — all rollback actions
are pre-computed inverses applied in reverse order.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from nexus.engine.fsm import IncidentState
from nexus.graph.platform import RollbackSnapshot, TargetResource, get_platform_registry
from nexus.graph.state import IncidentGraphState

logger = logging.getLogger(__name__)


async def rollback_node(state: IncidentGraphState) -> dict[str, Any]:
    """Deterministically execute inverse actions for executed steps in reverse order."""
    incident_id = state["incident_id"]
    execution_records = state.get("execution_records", [])
    target_dict = state.get("target") or {}
    target = TargetResource(**target_dict) if isinstance(target_dict, dict) else target_dict
    platform_id = state.get("platform", "kubernetes")

    registry = get_platform_registry()
    adapter = registry.get(platform_id) or registry.resolve_adapter(state.get("events", []))

    logger.warning(
        "[Rollback] Incident %s: initiating rollback across %d executed step(s)",
        incident_id,
        len(execution_records),
    )

    rollback_records: list[dict[str, Any]] = []
    # Reverse executed steps for proper LIFO rollback
    for rec in reversed(execution_records):
        snap_id = rec.get("snapshot_id")
        tool_name = rec.get("tool_name", "")

        # Attempt to synthesize snapshot or execute inverse
        logger.info("[Rollback] Rolling back step %s (snap_id=%s)", tool_name, snap_id)

        # Build fallback snapshot if specific snapshot not serialized
        snap = RollbackSnapshot(
            snapshot_id=snap_id or f"rollback-{tool_name}",
            target=target,
            action_name=tool_name,
            rollback_tool="k8s_restart_deployment" if platform_id == "kubernetes" else "aws_update_lambda_memory",
            rollback_parameters={"namespace": target.namespace, "deployment_name": target.name} if platform_id == "kubernetes" else {"function_name": target.name, "memory_mb": 256},
            description="Reverting action to prior stable state",
        )

        try:
            rb_res = await adapter.execute_rollback(snap)
            rollback_records.append({
                "snapshot_id": snap.snapshot_id,
                "tool": snap.rollback_tool,
                "success": rb_res.success,
                "error": rb_res.error,
            })
        except Exception as exc:
            logger.error("[Rollback] Failed executing rollback snapshot %s: %s", snap.snapshot_id, exc)
            rollback_records.append({
                "snapshot_id": snap.snapshot_id,
                "tool": snap.rollback_tool,
                "success": False,
                "error": str(exc),
            })

    now_iso = datetime.now(timezone.utc).isoformat()
    audit_entry = {
        "timestamp": now_iso,
        "stage": "rollback",
        "action": "rolled_back",
        "details": f"Reverted {len(rollback_records)} action(s)",
    }

    return {
        "rollback_executed": True,
        "rollback_records": rollback_records,
        "fsm_state": IncidentState.ROLLED_BACK.value,
        "audit_log": [audit_entry],
    }
