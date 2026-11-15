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
import zlib
from datetime import datetime, timezone
from typing import Any

from nexus.db import get_database_client
from nexus.engine.fsm import IncidentState
from nexus.graph.state import IncidentGraphState

logger = logging.getLogger(__name__)


async def learn_node(state: IncidentGraphState) -> dict[str, Any]:
    """Mark incident resolved, write audit trail, and release locks."""
    incident_id = state["incident_id"]
    diagnosis = state.get("diagnosis", {})
    target = state.get("target", {})
    platform = state.get("platform", "kubernetes")
    target_service = (
        target.get("name")
        if isinstance(target, dict)
        else getattr(target, "name", "unknown")
    )

    # 1. Release advisory lock if held (defense-in-depth safety release)
    if target_service:
        try:
            lock_key = zlib.crc32(str(target_service).encode())
            db_client = await get_database_client()
            async with db_client.acquire() as conn:
                if hasattr(conn, "execute"):
                    await conn.execute("SELECT pg_advisory_unlock($1)", lock_key)
        except Exception:
            pass

    # 2. Persist comprehensive post-incident record into PostgreSQL audit table
    try:
        from nexus.governance.audit_trail import AuditTrail

        trail = AuditTrail()
        await trail.initialize()
        await trail.write(
            triggered_by=f"nexus:workflow:{platform}",
            runbook_id=str(
                (state.get("plan") or {}).get("runbook_id") or "autonomous_remediation"
            ),
            healing_level=1,
            target=str(target_service),
            pre_check_results={"rca": diagnosis},
            execution_outcome="success",
            post_check_results={"verification": state.get("verification", {})},
            incident_id=incident_id,
            action_results=state.get("execution_records", []),
        )
    except Exception as audit_err:
        logger.debug("[Learn] AuditTrail write skipped: %s", audit_err)

    now_iso = datetime.now(timezone.utc).isoformat()
    audit_entry = {
        "timestamp": now_iso,
        "stage": "learn",
        "action": "incident_resolved_and_indexed",
        "details": f"Successfully healed {target_service} ({platform}) via {diagnosis.get('failure_class') if isinstance(diagnosis, dict) else getattr(diagnosis, 'failure_class', 'unknown')}",
    }

    logger.info(
        "[Learn] Incident %s recorded into learning plane: target=%s, platform=%s",
        incident_id,
        target_service,
        platform,
    )

    return {
        "resolved": True,
        "fsm_state": IncidentState.RESOLVED.value,
        "audit_log": [audit_entry],
    }
