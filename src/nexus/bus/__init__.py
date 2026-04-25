"""
nexus.bus — Normalized Incident Event Schema + NATS JetStream Client

The event bus is the nervous system of NEXUS. Every domain agent
transforms its raw observation into an IncidentEvent before publishing.
The Orchestrator and Runbook Executor are the sole consumers.

Subjects follow the pattern:
    nexus.incidents.<agent>.<signal_type>

Example:
    nexus.incidents.k8s.pod_crashloop
    nexus.incidents.metrics.anomaly_detected
    nexus.incidents.git.env_contract_violation
"""

from nexus.bus.incident_event import (
    IncidentEvent,
    AgentType,
    SignalType,
    Severity,
    HealingLevel,
)
from nexus.bus.nats_client import NATSClient

__all__ = [
    "IncidentEvent",
    "AgentType",
    "SignalType",
    "Severity",
    "HealingLevel",
    "NATSClient",
]
