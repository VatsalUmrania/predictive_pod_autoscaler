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

from langchain_core.messages import HumanMessage

from nexus.engine.fsm import IncidentState
from nexus.graph.platform import get_platform_registry
from nexus.graph.platform.base import TargetResource
from nexus.graph.state import IncidentGraphState

logger = logging.getLogger(__name__)


def triage_node(state: IncidentGraphState) -> dict[str, Any]:
    """Triage incoming incident signals and resolve platform adapter."""
    events = state.get("events", [])
    anomaly_signal = state.get("anomaly_signal")
    registry = get_platform_registry()
    initial_messages: list[Any] = []
    telemetry_dict: dict[str, Any] = state.get("telemetry") or {}

    if anomaly_signal:
        signal_dict = (
            anomaly_signal.model_dump()
            if hasattr(anomaly_signal, "model_dump")
            else dict(anomaly_signal)
        )
        provider = signal_dict.get("provider", "kubernetes")
        service = signal_dict.get("target_service", "unknown")
        environment = signal_dict.get("environment", "production")
        symptom = signal_dict.get("symptom_summary", "")
        resource_id = signal_dict.get("target_resource_id", service)
        raw_metrics = signal_dict.get("raw_metrics_or_alert", {})

        platform_id = "aws" if provider == "aws" else "kubernetes"
        target = TargetResource(
            platform=platform_id,
            namespace=environment if platform_id == "aws" else "default",
            name=service,
            kind="service",
            arn_or_uri=resource_id if ":" in str(resource_id) else None,
        )
        severity = (
            "critical"
            if any(
                w in symptom.lower() for w in ("500", "critical", "throttl", "crash")
            )
            else "warning"
        )
        initial_messages = [
            HumanMessage(
                content=f"AnomalySignal [{signal_dict.get('source', 'alert')}]: "
                f"{symptom} on service '{service}' ({platform_id}, {environment}). "
                f"Raw metrics: {raw_metrics}"
            )
        ]
    else:
        signal_dict = None
        adapter = registry.resolve_adapter(events)
        platform_id = adapter.platform_id
        target = adapter.detect_target(events)
        severity = "warning"
        for evt in events:
            s = str(evt.get("severity", "")).lower()
            if s == "critical":
                severity = "critical"
                break
            if s in ("error", "high"):
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
        state.get("incident_id"),
        target,
        platform_id,
        severity,
    )

    update: dict[str, Any] = {
        "platform": platform_id,
        "target": target.model_dump(),
        "severity": severity,
        "telemetry": telemetry_dict,
        "fsm_state": IncidentState.CORRELATED.value,
        "audit_log": [audit_entry],
        "fallback_active": False,
    }
    if initial_messages:
        update["messages"] = initial_messages
    if signal_dict is not None:
        update["anomaly_signal"] = signal_dict

    return update
