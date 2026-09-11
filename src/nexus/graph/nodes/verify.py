"""
NEXUS Post-Execution Verification Node
======================================
Verifies that target SLO and health are restored following remediation execution.
Integrates with the platform adapter's verify_health() contract and updates
the Governance Circuit Breaker failure counts.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from nexus.engine.fsm import IncidentState
from nexus.graph.platform import TargetResource, get_platform_registry
from nexus.graph.state import IncidentGraphState

logger = logging.getLogger(__name__)


async def verify_node(state: IncidentGraphState) -> dict[str, Any]:
    """Verify health and SLO recovery via the platform adapter."""
    incident_id = state["incident_id"]
    target_dict = state.get("target") or {}
    target = TargetResource(**target_dict) if isinstance(target_dict, dict) else target_dict
    platform_id = state.get("platform", "kubernetes")
    plan = state.get("plan") or {}

    registry = get_platform_registry()
    adapter = registry.get(platform_id) or registry.resolve_adapter(state.get("events", []))

    # Brief settle delay to allow platform controllers/pods to stabilize
    await asyncio.sleep(0.5)

    try:
        verif_result = await adapter.verify_health(target, plan)
        healthy = verif_result.healthy
        slo_restored = verif_result.slo_restored
        details = verif_result.details
        failure_reason = verif_result.failure_reason
    except Exception as exc:
        logger.warning("[Verify] Verification error for %s: %s", target, exc)
        healthy = False
        slo_restored = False
        details = f"Verification exception: {exc}"
        failure_reason = str(exc)

    retry_count = state.get("retry_count", 0)
    max_retries = state.get("max_retries", 2)
    now_iso = datetime.now(timezone.utc).isoformat()

    if healthy and slo_restored:
        logger.info("[Verify] Incident %s VERIFIED: target %s healthy and SLO restored", incident_id, target)
        next_fsm = IncidentState.RESOLVED.value
        audit_entry = {
            "timestamp": now_iso,
            "stage": "verify",
            "action": "slo_restored",
            "details": f"Target {target} verified healthy: {details}",
        }
        return {
            "verification": {
                "healthy": True,
                "slo_restored": True,
                "details": details,
            },
            "resolved": True,
            "fsm_state": next_fsm,
            "audit_log": [audit_entry],
        }

    # Verification failed
    logger.warning(
        "[Verify] Incident %s verification FAILED for %s (reason: %s). Attempt %d/%d",
        incident_id,
        target,
        failure_reason,
        retry_count + 1,
        max_retries,
    )

    if retry_count < max_retries:
        next_fsm = IncidentState.RETRYING.value
        audit_entry = {
            "timestamp": now_iso,
            "stage": "verify",
            "action": "retry_scheduled",
            "details": f"Health check failed ({failure_reason}) — scheduling retry ({retry_count + 1}/{max_retries})",
        }
        return {
            "verification": {
                "healthy": False,
                "slo_restored": False,
                "details": details,
                "failure_reason": failure_reason,
            },
            "retry_count": retry_count + 1,
            "fsm_state": next_fsm,
            "audit_log": [audit_entry],
        }

    # Max retries exceeded -> Trigger Rollback
    next_fsm = IncidentState.ROLLING_BACK.value
    audit_entry = {
        "timestamp": now_iso,
        "stage": "verify",
        "action": "max_retries_exceeded",
        "details": f"Health check failed after {max_retries} attempts — initiating deterministic rollback",
    }

    return {
        "verification": {
            "healthy": False,
            "slo_restored": False,
            "details": details,
            "failure_reason": failure_reason,
        },
        "fsm_state": next_fsm,
        "audit_log": [audit_entry],
    }
