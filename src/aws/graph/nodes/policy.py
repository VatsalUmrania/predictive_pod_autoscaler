"""
Policy Node
============
Safety gate between the LLM recommendation and actual AWS API calls.

Checks:
  1. Is the action in the allowed whitelist?
  2. Is the confidence above the auto-execute threshold?
  3. Are the action parameters within safe bounds?

If all checks pass → approved = True → graph routes to execute node.
If any check fails → approved = False → graph routes to escalate node.

This node never touches AWS. It only inspects state["rca"] and state["context"].
"""

from __future__ import annotations

import logging

from aws.config import AgentConfig
from aws.graph.state import AgentState

logger = logging.getLogger(__name__)

# ── Action whitelist ──────────────────────────────────────────────────────────
# Each entry: action_name → {max_param_value, description}
# The LLM is only told about these actions in the system prompt.

ALLOWED_ACTIONS = {
    "alert_only":            {"description": "L0 — notify only, no AWS changes"},
    "increase_memory":       {"description": "L2 — increase Lambda memory", "max_memory_mb": 3008},
    "increase_timeout":      {"description": "L2 — increase Lambda timeout", "max_timeout_seconds": 900},
    "set_concurrency":       {"description": "L2 — set Lambda reserved concurrency", "max_concurrency": 1000},
    "rollback_alias":        {"description": "L3 — roll back Lambda alias to previous version"},
    "replay_dlq":            {"description": "L2 — replay DLQ messages to source queue"},
}

# L3 actions require higher confidence (always human-approved if below threshold)
L3_ACTIONS = {"rollback_alias"}


def policy(state: AgentState) -> dict:
    """
    LangGraph node: validate RCA recommendation against safety policy.

    Input state fields used:  rca, context
    Output state fields set:  approved, policy_reason
    """
    cfg = AgentConfig.load()
    rca = state.get("rca") or {}
    action = rca.get("action", "alert_only")
    confidence = float(rca.get("confidence", 0.0))
    params = rca.get("params", {})

    logger.info(f"[Policy] Evaluating: action={action}, confidence={confidence:.0%}")

    # ── Check 1: Action must be in whitelist ──────────────────────────────────
    if action not in ALLOWED_ACTIONS:
        reason = f"Action '{action}' is not in the allowed action whitelist."
        logger.warning(f"[Policy] BLOCKED — {reason}")
        return {"approved": False, "policy_reason": reason}

    # ── Check 2: alert_only is always approved (no AWS changes) ──────────────
    if action == "alert_only":
        return {"approved": True, "policy_reason": "Alert-only action — always approved."}

    # ── Check 3: Confidence threshold ────────────────────────────────────────
    threshold = cfg.confidence_threshold
    if confidence < threshold:
        reason = (
            f"Confidence {confidence:.0%} is below auto-execute threshold {threshold:.0%}. "
            f"Human approval required for action '{action}'."
        )
        logger.warning(f"[Policy] BLOCKED — {reason}")
        return {"approved": False, "policy_reason": reason}

    # ── Check 4: Parameter bounds ─────────────────────────────────────────────
    policy_meta = ALLOWED_ACTIONS[action]

    if action == "increase_memory":
        memory = int(params.get("memory_mb", 512))
        max_memory = policy_meta["max_memory_mb"]
        if memory > max_memory:
            reason = f"Requested memory {memory}MB exceeds max allowed {max_memory}MB."
            logger.warning(f"[Policy] BLOCKED — {reason}")
            return {"approved": False, "policy_reason": reason}

    elif action == "increase_timeout":
        timeout = int(params.get("timeout_seconds", 30))
        max_timeout = policy_meta["max_timeout_seconds"]
        if timeout > max_timeout:
            reason = f"Requested timeout {timeout}s exceeds max allowed {max_timeout}s."
            logger.warning(f"[Policy] BLOCKED — {reason}")
            return {"approved": False, "policy_reason": reason}

    elif action == "set_concurrency":
        concurrency = int(params.get("reserved_concurrent_executions", 100))
        max_concurrency = policy_meta["max_concurrency"]
        if concurrency > max_concurrency:
            reason = f"Requested concurrency {concurrency} exceeds max allowed {max_concurrency}."
            logger.warning(f"[Policy] BLOCKED — {reason}")
            return {"approved": False, "policy_reason": reason}

    # ── All checks passed ─────────────────────────────────────────────────────
    reason = (
        f"Action '{action}' approved: confidence={confidence:.0%} ≥ threshold={threshold:.0%}, "
        f"parameters within bounds."
    )
    logger.info(f"[Policy] APPROVED — {reason}")
    return {"approved": True, "policy_reason": reason}
