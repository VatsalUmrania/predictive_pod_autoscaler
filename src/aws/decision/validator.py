"""
Phase 6 — Decision Engine: Validator
======================================
Validates LLM output before any boto3 call.

The LLM recommends. The validator decides if it's safe.

Checks:
  1. Action is in the allowed whitelist
  2. Parameters are within safe bounds
  3. Confidence meets the threshold
  4. Resource name is not a protected resource

GPT → JSON → Validator → (approved) → Planner → Executor → AWS
                        → (blocked)  → Escalate
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from aws.config import AgentConfig

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    approved: bool
    reason: str
    risk_level: str    # "low" | "medium" | "high"


# ── Action whitelist with safety bounds ──────────────────────────────────────
ALLOWED_ACTIONS: dict[str, dict] = {
    "alert_only": {
        "description": "L0 — notify only, no AWS changes",
        "risk": "low",
    },
    "increase_memory": {
        "description": "L2 — increase Lambda memory",
        "risk": "low",
        "max_memory_mb": 3008,
        "min_memory_mb": 128,
    },
    "increase_timeout": {
        "description": "L2 — increase Lambda timeout",
        "risk": "low",
        "max_timeout_seconds": 900,
    },
    "set_concurrency": {
        "description": "L2 — set Lambda reserved concurrency",
        "risk": "medium",
        "max_concurrency": 1000,
    },
    "rollback_alias": {
        "description": "L3 — roll back Lambda alias",
        "risk": "high",
    },
    "replay_dlq": {
        "description": "L2 — replay SQS DLQ messages",
        "risk": "medium",
    },
}

# Resources that should NEVER be auto-remediated
PROTECTED_RESOURCES: set[str] = {
    "production-critical",
    "nexus-ai-agent",    # Don't let the agent remediate itself
}


def validate(rca: dict[str, Any], cfg: AgentConfig | None = None) -> ValidationResult:
    """
    Validate the LLM's RCA recommendation.

    Args:
        rca: Output from diagnosis_agent.diagnose()
        cfg: AgentConfig (loaded from env if None)

    Returns:
        ValidationResult with approved=True/False and reason.
    """
    cfg = cfg or AgentConfig.load()
    action = rca.get("action", "alert_only")
    confidence = float(rca.get("confidence", 0.0))
    params = rca.get("params", {})
    resource = params.get("function_name", rca.get("service", ""))

    # ── Check 1: Action must be whitelisted ───────────────────────────────────
    if action not in ALLOWED_ACTIONS:
        return ValidationResult(
            approved=False,
            reason=f"Action '{action}' is not in the allowed action whitelist.",
            risk_level="high",
        )

    # ── Check 2: alert_only is always safe ───────────────────────────────────
    if action == "alert_only":
        return ValidationResult(approved=True, reason="Alert-only — no AWS changes.", risk_level="low")

    # ── Check 3: Protected resources ──────────────────────────────────────────
    for protected in PROTECTED_RESOURCES:
        if protected in resource:
            return ValidationResult(
                approved=False,
                reason=f"Resource '{resource}' matches protected resource pattern '{protected}'.",
                risk_level="high",
            )

    # ── Check 4: Confidence threshold ─────────────────────────────────────────
    threshold = cfg.confidence_threshold
    if confidence < threshold:
        return ValidationResult(
            approved=False,
            reason=(
                f"Confidence {confidence:.0%} is below auto-execute threshold {threshold:.0%}. "
                f"Human approval required for action '{action}'."
            ),
            risk_level=ALLOWED_ACTIONS[action]["risk"],
        )

    # ── Check 5: Parameter bounds ─────────────────────────────────────────────
    meta = ALLOWED_ACTIONS[action]

    if action == "increase_memory":
        mb = int(params.get("memory_mb", 512))
        if mb > meta["max_memory_mb"]:
            return ValidationResult(
                approved=False,
                reason=f"Requested memory {mb}MB exceeds maximum {meta['max_memory_mb']}MB.",
                risk_level="medium",
            )
        if mb < meta["min_memory_mb"]:
            return ValidationResult(
                approved=False,
                reason=f"Requested memory {mb}MB is below minimum {meta['min_memory_mb']}MB.",
                risk_level="low",
            )

    elif action == "increase_timeout":
        ts = int(params.get("timeout_seconds", 30))
        if ts > meta["max_timeout_seconds"]:
            return ValidationResult(
                approved=False,
                reason=f"Requested timeout {ts}s exceeds maximum {meta['max_timeout_seconds']}s.",
                risk_level="medium",
            )

    elif action == "set_concurrency":
        c = int(params.get("reserved_concurrent_executions", 100))
        if c > meta["max_concurrency"]:
            return ValidationResult(
                approved=False,
                reason=f"Requested concurrency {c} exceeds maximum {meta['max_concurrency']}.",
                risk_level="medium",
            )

    # ── All checks passed ─────────────────────────────────────────────────────
    risk = meta["risk"]
    return ValidationResult(
        approved=True,
        reason=f"Approved: confidence={confidence:.0%} ≥ threshold={threshold:.0%}, params within bounds.",
        risk_level=risk,
    )
