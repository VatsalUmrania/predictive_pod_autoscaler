"""
NEXUS Post-Execution Verification Node
======================================
Verifies that target SLO and health are restored following remediation execution.
Polls the platform adapter's verify_health() contract until healthy or the window
expires (rollout restarts take ~30s+), and updates the Governance Circuit Breaker:
  - healthy      → record_post_check_success + set cooldown for the action/target
  - unhealthy    → record_post_check_failure (may trip the breaker after N failures)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from nexus.engine.fsm import IncidentState
from nexus.graph.platform import TargetResource, get_platform_registry
from nexus.graph.state import IncidentGraphState

logger = logging.getLogger(__name__)

# Poll window: real remediations (rollout restarts, config changes) take tens of
# seconds to stabilize. Override via env for tests/tuning.
VERIFY_TIMEOUT_S = float(os.getenv("NEXUS_VERIFY_TIMEOUT_S", "120"))
VERIFY_POLL_INTERVAL_S = float(os.getenv("NEXUS_VERIFY_POLL_INTERVAL_S", "5"))


def _get_ladder() -> Any:
    """Fetch the ActionLadder from the runtime context (None outside the server)."""
    try:
        from nexus.observability.status_api import context

        return context.action_ladder
    except Exception:
        return None


def _remediation_action_id(state: IncidentGraphState, plan: dict[str, Any]) -> str:
    """Identity of the mutation just executed (last executed step, else first planned)."""
    records = state.get("execution_records") or []
    if records and isinstance(records[-1], dict) and records[-1].get("tool_name"):
        return str(records[-1]["tool_name"])
    steps = plan.get("steps") or []
    if steps and isinstance(steps[0], dict) and steps[0].get("tool_name"):
        return str(steps[0]["tool_name"])
    return "unknown"


async def verify_node(state: IncidentGraphState) -> dict[str, Any]:
    """Poll target health/SLO via the platform adapter until healthy or window expires."""
    incident_id = state["incident_id"]
    target_dict = state.get("target") or {}
    target = TargetResource(**target_dict) if isinstance(target_dict, dict) else target_dict
    platform_id = state.get("platform", "kubernetes")
    plan = state.get("plan") or {}

    registry = get_platform_registry()
    adapter = registry.get(platform_id) or registry.resolve_adapter(state.get("events", []))

    healthy = False
    slo_restored = False
    details = ""
    failure_reason = ""
    attempts = 0
    deadline = time.monotonic() + VERIFY_TIMEOUT_S

    # Poll loop: first check immediately (fast path), then wait for stabilization.
    while True:
        attempts += 1
        try:
            verif_result = await adapter.verify_health(target, plan)
            healthy = verif_result.healthy
            slo_restored = verif_result.slo_restored
            details = verif_result.details
            failure_reason = verif_result.failure_reason or ""
        except Exception as exc:
            logger.warning("[Verify] Verification error for %s: %s", target, exc)
            healthy = False
            slo_restored = False
            details = f"Verification exception: {exc}"
            failure_reason = str(exc)

        if healthy and slo_restored:
            break
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(VERIFY_POLL_INTERVAL_S)

    retry_count = state.get("retry_count", 0)
    max_retries = state.get("max_retries", 2)
    now_iso = datetime.now(timezone.utc).isoformat()

    action_id = _remediation_action_id(state, plan)
    target_str = f"{target.namespace}/{target.name}"
    ladder = _get_ladder()

    if healthy and slo_restored:
        logger.info("[Verify] Incident %s VERIFIED: target %s healthy after %d check(s)", incident_id, target, attempts)

        if ladder is not None:
            try:
                ladder.record_post_check_success()
                await ladder.set_cooldown(action_id, target_str)
            except Exception as cb_exc:
                logger.warning("[Verify] Circuit-breaker/cooldown update failed: %s", cb_exc)

        audit_entry = {
            "timestamp": now_iso,
            "stage": "verify",
            "action": "slo_restored",
            "details": f"Target {target} verified healthy after {attempts} check(s): {details}",
        }
        return {
            "verification": {
                "healthy": True,
                "slo_restored": True,
                "details": details,
                "attempts": attempts,
            },
            "resolved": True,
            "fsm_state": IncidentState.RESOLVED.value,
            "audit_log": [audit_entry],
        }

    # Verification failed — a failed post-check counts toward the governance circuit breaker
    if ladder is not None:
        try:
            ladder.record_post_check_failure()
        except Exception as cb_exc:
            logger.warning("[Verify] Circuit-breaker failure record failed: %s", cb_exc)

    logger.warning(
        "[Verify] Incident %s verification FAILED for %s after %d check(s) (reason: %s). Attempt %d/%d",
        incident_id,
        target,
        attempts,
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
            "details": f"Health check failed after {attempts} check(s) ({failure_reason}) — scheduling retry ({retry_count + 1}/{max_retries})",
        }
        return {
            "verification": {
                "healthy": False,
                "slo_restored": False,
                "details": details,
                "failure_reason": failure_reason,
                "attempts": attempts,
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
        "details": f"Health check failed after {attempts} check(s) over {VERIFY_TIMEOUT_S}s — initiating deterministic rollback",
    }

    return {
        "verification": {
            "healthy": False,
            "slo_restored": False,
            "details": details,
            "failure_reason": failure_reason,
            "attempts": attempts,
        },
        "fsm_state": next_fsm,
        "audit_log": [audit_entry],
    }
