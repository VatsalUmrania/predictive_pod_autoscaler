"""
Initial graph state builder for IncidentWorkflow.
"""

from __future__ import annotations

from typing import Any

from nexus.db.models import IncidentState
from nexus.graph.state import IncidentGraphState


def build_initial_state(
    incident_data: dict[str, Any], inc_id: str
) -> IncidentGraphState:
    """Construct initial state dictionary for incident workflow."""
    events = incident_data.get("events", [])
    if not events and "event" in incident_data:
        events = [incident_data["event"]]
    anomaly_signal = incident_data.get("anomaly_signal")
    if anomaly_signal and hasattr(anomaly_signal, "model_dump"):
        anomaly_signal = anomaly_signal.model_dump()

    return {
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
        "messages": incident_data.get("messages", []),
        "error_message": None,
        "resolved": False,
        "escalated": False,
        "anomaly_signal": anomaly_signal,
        "agentic_rca": incident_data.get("agentic_rca"),
        "fallback_active": incident_data.get("fallback_active", False),
    }
