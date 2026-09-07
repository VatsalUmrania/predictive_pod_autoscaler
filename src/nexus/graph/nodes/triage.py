"""
NEXUS Triage Node
=================
First node in the LangGraph incident pipeline.
Resolves platform via PlatformRegistry, identifies target resource,
evaluates incident severity, and sets FSM state to CORRELATED.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from nexus.engine.fsm import IncidentState
from nexus.graph.platform import get_platform_registry
from nexus.graph.state import IncidentGraphState

logger = logging.getLogger(__name__)


def triage_node(state: IncidentGraphState) -> dict[str, Any]:
    """Triage incoming incident signals and resolve platform adapter."""
    events = state.get("events", [])
    registry = get_platform_registry()

    # 1. Resolve Platform Adapter
    adapter = registry.resolve_adapter(events)
    platform_id = adapter.platform_id

    # 2. Extract Normalized Target Resource
    target = adapter.detect_target(events)

    # 3. Determine Highest Severity
    severity = "warning"
    for evt in events:
        s = str(evt.get("severity", "")).lower()
        if s == "critical":
            severity = "critical"
            break
        elif s in ("error", "high"):
            severity = "error"

    now_iso = datetime.now(timezone.utc).isoformat()
    audit_entry = {
        "timestamp": now_iso,
        "stage": "triage",
        "action": "classified_target",
        "details": f"Target: {target} | Platform: {platform_id} | Severity: {severity}",
    }

    logger.info(
        "[Triage] Incident %s triaged: target=%s, platform=%s, severity=%s",
        state["incident_id"],
        target,
        platform_id,
        severity,
    )

    return {
        "platform": platform_id,
        "target": target.model_dump(),
        "severity": severity,
        "fsm_state": IncidentState.CORRELATED.value,
        "audit_log": [audit_entry],
    }
