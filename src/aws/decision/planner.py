"""
Phase 6 — Decision Engine: Planner
=====================================
Translates the LLM's recommendation into a concrete execution plan.

The planner builds a step-by-step action plan with:
  - Primary action (from LLM)
  - Fallback actions (if primary fails or doesn't resolve)
  - Rollback plan (how to undo the action)

This ensures the executor always has a clear, ordered list of steps.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ActionStep:
    """A single step in the execution plan."""
    action: str
    params: dict[str, Any]
    description: str
    is_fallback: bool = False
    is_rollback: bool = False


@dataclass
class ExecutionPlan:
    """Ordered list of steps to execute for an incident."""
    resource_name: str
    primary_action: ActionStep
    fallback_steps: list[ActionStep] = field(default_factory=list)
    rollback_step: ActionStep | None = None
    dry_run: bool = False


# Predefined fallback chains for each action
_FALLBACK_CHAINS: dict[str, list[dict]] = {
    "increase_memory": [
        {
            "action": "increase_timeout",
            "params_fn": lambda p: {"timeout_seconds": 60},
            "description": "Fallback: increase timeout in case issue persists",
        }
    ],
    "increase_timeout": [
        {
            "action": "increase_memory",
            "params_fn": lambda p: {"memory_mb": 512},
            "description": "Fallback: increase memory — may speed up execution",
        }
    ],
    "rollback_alias": [],  # No automatic fallback for rollbacks
    "set_concurrency": [],
    "replay_dlq": [],
    "alert_only": [],
}


def build_plan(
    resource_name: str,
    rca: dict[str, Any],
    dry_run: bool = False,
) -> ExecutionPlan:
    """
    Build an execution plan from the LLM's RCA output.

    Args:
        resource_name: The resource to act on
        rca:           Output from diagnosis_agent.diagnose()
        dry_run:       If True, mark plan as dry-run

    Returns:
        ExecutionPlan with primary, fallback, and rollback steps.
    """
    action = rca.get("action", "alert_only")
    params = dict(rca.get("params", {}))

    # Ensure function_name is in params if not already
    if action not in ("replay_dlq", "alert_only"):
        params.setdefault("function_name", resource_name)

    primary = ActionStep(
        action=action,
        params=params,
        description=rca.get("recommendations", [action])[0] if rca.get("recommendations") else action,
    )

    # Build fallback steps
    fallback_chain = _FALLBACK_CHAINS.get(action, [])
    fallback_steps = [
        ActionStep(
            action=step["action"],
            params=step["params_fn"](params),
            description=step["description"],
            is_fallback=True,
        )
        for step in fallback_chain
    ]

    # Build rollback step (undo the primary action)
    rollback = _build_rollback(action, params, resource_name)

    logger.info(
        f"[Planner] Plan for {resource_name}: "
        f"primary={action}, fallbacks={len(fallback_steps)}, "
        f"has_rollback={rollback is not None}"
    )

    return ExecutionPlan(
        resource_name=resource_name,
        primary_action=primary,
        fallback_steps=fallback_steps,
        rollback_step=rollback,
        dry_run=dry_run,
    )


def _build_rollback(action: str, params: dict, resource_name: str) -> ActionStep | None:
    """Build a rollback step that undoes the primary action."""
    if action == "increase_memory":
        # Rollback: restore original memory (we don't know it here, so note it)
        return ActionStep(
            action="alert_only",
            params={"note": "Restore original memory size if issues persist post-action"},
            description="If action worsens the incident, manually restore original memory size.",
            is_rollback=True,
        )
    if action == "rollback_alias":
        return ActionStep(
            action="alert_only",
            params={"note": "Re-deploy the version if rollback causes issues"},
            description="If rollback causes new issues, re-deploy the original version.",
            is_rollback=True,
        )
    return None
