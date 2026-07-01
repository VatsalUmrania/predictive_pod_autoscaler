"""
Phase 10 — Multi-Agent: Supervisor
=====================================
Routes incoming incidents to the correct specialized agent.

The Supervisor reads the signal_type and decides which combination
of agents to invoke. This is the orchestration layer above LangGraph.

For simple incidents: Monitor → Diagnose → Remediate → Verify
For unknown signals: Monitor → Diagnose → Escalate

In the LangGraph workflow, the supervisor runs as the entry decision.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# Signal type → list of agents to invoke (in order)
_ROUTING_TABLE: dict[str, list[str]] = {
    "lambda_error_rate_high":  ["diagnosis", "remediation", "verification"],
    "lambda_throttle_spike":   ["diagnosis", "remediation", "verification"],
    "lambda_timeout":          ["diagnosis", "remediation", "verification"],
    "lambda_oom":              ["diagnosis", "remediation", "verification"],
    "sqs_dlq_depth_high":      ["diagnosis", "remediation", "verification"],
    "dynamo_throttle_read":    ["diagnosis"],          # No auto-remediation for DynamoDB
    "dynamo_throttle_write":   ["diagnosis"],          # Human decides on capacity changes
    "apigw_5xx_spike":         ["diagnosis", "remediation", "verification"],
    "aws_alarm_fired":         ["diagnosis"],          # Unknown alarm — diagnose only
}

_DEFAULT_PIPELINE = ["diagnosis", "remediation", "verification"]


def get_pipeline(signal_type: str) -> list[str]:
    """
    Return the ordered list of agents to run for a given signal type.
    Falls back to diagnose-only for unknown signal types.
    """
    pipeline = _ROUTING_TABLE.get(signal_type)
    if pipeline is None:
        logger.warning(f"[Supervisor] Unknown signal '{signal_type}' — using diagnose-only pipeline")
        return ["diagnosis"]
    return pipeline


def route_incident(incident: dict[str, Any]) -> dict[str, Any]:
    """
    Annotate the incident dict with routing metadata.
    Returns the incident dict with 'pipeline' and 'agent_count' added.
    """
    signal = incident.get("signal_type", "aws_alarm_fired")
    pipeline = get_pipeline(signal)

    logger.info(
        f"[Supervisor] Routing {incident.get('resource_name', 'unknown')} "
        f"({signal}) → pipeline: {' → '.join(pipeline)}"
    )

    return {
        **incident,
        "pipeline": pipeline,
        "agent_count": len(pipeline),
        "routed": True,
    }


def should_auto_remediate(signal_type: str) -> bool:
    """Return True if the signal type is in the auto-remediation whitelist."""
    pipeline = _ROUTING_TABLE.get(signal_type, [])
    return "remediation" in pipeline
