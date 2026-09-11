"""
NEXUS Collect Telemetry Node (Deterministic Context Acquisition)
================================================================
Gathers target metrics, recent error logs, and live configuration through the
platform adapter in a single deterministic step.

CRITICAL DESIGN NOTE:
This node explicitly replaces non-deterministic, expensive ReAct conversational loops.
All platform diagnostic telemetry is deterministically collected and structured
before any LLM or rule-based reasoning engine is invoked.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from nexus.engine.fsm import IncidentState
from nexus.graph.platform import TargetResource, get_platform_registry
from nexus.graph.state import IncidentGraphState

logger = logging.getLogger(__name__)


async def collect_telemetry_node(state: IncidentGraphState) -> dict[str, Any]:
    """Deterministically collect diagnostic signals via the platform adapter."""
    registry = get_platform_registry()
    platform_id = state.get("platform", "kubernetes")
    adapter = registry.get(platform_id) or registry.resolve_adapter(state.get("events", []))

    raw_target = state.get("target") or {}
    target = TargetResource(**raw_target) if isinstance(raw_target, dict) else raw_target

    logger.info(
        "[CollectTelemetry] Deterministically gathering telemetry for %s on platform %s",
        target,
        platform_id,
    )

    try:
        telemetry = await adapter.collect_telemetry(target)
        telemetry_dict = telemetry.model_dump()
    except Exception as exc:
        logger.warning(
            "[CollectTelemetry] Adapter failed to gather telemetry for %s: %s", target, exc
        )
        telemetry_dict = {
            "target": target.model_dump() if hasattr(target, "model_dump") else {},
            "metrics": {},
            "recent_logs": [f"Telemetry collection error: {exc}"],
            "live_config": {},
            "health_status": "error",
            "raw_signals": state.get("events", []),
        }

    now_iso = datetime.now(timezone.utc).isoformat()
    audit_entry = {
        "timestamp": now_iso,
        "stage": "collect_telemetry",
        "action": "telemetry_acquired",
        "details": f"Logs: {len(telemetry_dict.get('recent_logs', []))} lines | Status: {telemetry_dict.get('health_status')}",
    }

    return {
        "telemetry": telemetry_dict,
        "fsm_state": IncidentState.DIAGNOSING.value,
        "audit_log": [audit_entry],
    }
