"""NEXUS Incident Event Schema (Facade).

Backward-compatible entry point re-exporting event models and types.
"""
from nexus.bus.events import (
    AgentType,
    ConfigDriftContext,
    DBConnectionExhaustionContext,
    DeployEventContext,
    DNSFailureContext,
    EnvContractViolationContext,
    HealingLevel,
    IncidentEvent,
    MetricsAnomalyContext,
    NginxHighErrorContext,
    PodFailureContext,
    Severity,
    SignalType,
    TrafficSpikePredictionContext,
)

__all__ = [
    "AgentType",
    "ConfigDriftContext",
    "DBConnectionExhaustionContext",
    "DNSFailureContext",
    "DeployEventContext",
    "EnvContractViolationContext",
    "HealingLevel",
    "IncidentEvent",
    "MetricsAnomalyContext",
    "NginxHighErrorContext",
    "PodFailureContext",
    "Severity",
    "SignalType",
    "TrafficSpikePredictionContext",
]
