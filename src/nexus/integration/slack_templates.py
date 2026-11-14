"""Slack Block Kit templates facade for Notifier."""

from __future__ import annotations

from nexus.integration.slack_action_templates import (
    build_agentic_approval_payload,
    build_approval_required_payload,
    build_escalation_payload,
    build_heal_payload,
    build_prescale_payload,
)
from nexus.integration.slack_incident_templates import (
    build_incident_escalated_payload,
    build_incident_resolved_payload,
    build_report_ready_payload,
)

__all__ = [
    "build_heal_payload",
    "build_prescale_payload",
    "build_escalation_payload",
    "build_approval_required_payload",
    "build_agentic_approval_payload",
    "build_incident_resolved_payload",
    "build_incident_escalated_payload",
    "build_report_ready_payload",
]
