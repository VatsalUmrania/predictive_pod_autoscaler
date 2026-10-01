"""
NEXUS Closed-Loop Learning Node
===============================
Terminal learning stage of the graph: marks the incident resolved and records
the audit entry. Persistence (audit trail row, KnowledgeBase outcome + signal
pattern) happens in IncidentWorkflow._handle_workflow_completion — the single
choke point that sees ALL terminal outcomes, including failed/escalated ones.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from nexus.engine.fsm import IncidentState
from nexus.graph.state import IncidentGraphState

logger = logging.getLogger(__name__)


async def learn_node(state: IncidentGraphState) -> dict[str, Any]:
    """Mark incident resolved and emit the learn-stage audit entry."""
    incident_id = state["incident_id"]
    diagnosis = state.get("diagnosis", {})
    target = state.get("target", {})
    platform = state.get("platform", "kubernetes")

    now_iso = datetime.now(timezone.utc).isoformat()
    audit_entry = {
        "timestamp": now_iso,
        "stage": "learn",
        "action": "incident_resolved_and_indexed",
        "details": f"Successfully healed {target.get('name')} ({platform}) via {diagnosis.get('failure_class')}",
    }

    logger.info(
        "[Learn] Incident %s recorded into learning plane: target=%s, platform=%s",
        incident_id,
        target.get("name"),
        platform,
    )

    return {
        "resolved": True,
        "fsm_state": IncidentState.RESOLVED.value,
        "audit_log": [audit_entry],
    }
