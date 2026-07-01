"""
Phase 10 — Multi-Agent: Remediation Agent
==========================================
Executes the approved action plan.
Wraps the decision/executor with logging and pre/post state capture.
"""

from __future__ import annotations

import logging
from typing import Any

from aws.config import AgentConfig
from aws.decision import validator, planner, executor

logger = logging.getLogger(__name__)


def remediate(
    resource_name: str,
    rca: dict[str, Any],
    cfg: AgentConfig | None = None,
) -> dict[str, Any]:
    """
    Validate, plan, and execute the remediation for an incident.

    Pipeline: rca → validator → planner → executor → result

    Args:
        resource_name: AWS resource to remediate
        rca:           Output from diagnosis_agent.diagnose()
        cfg:           AgentConfig (loaded from env if None)

    Returns:
        {
          "approved": bool,
          "reason":   str,
          "plan":     {...},
          "result":   {...}  (from executor)
        }
    """
    cfg = cfg or AgentConfig.load()
    action = rca.get("action", "alert_only")

    logger.info(f"[RemediationAgent] Starting remediation: {resource_name} / {action}")

    # Step 1: Validate
    validation = validator.validate(rca, cfg)
    logger.info(f"[RemediationAgent] Validation: approved={validation.approved} reason={validation.reason}")

    if not validation.approved:
        return {
            "approved": False,
            "reason":   validation.reason,
            "risk":     validation.risk_level,
            "plan":     None,
            "result":   None,
        }

    # Step 2: Plan
    plan = planner.build_plan(resource_name, rca, dry_run=cfg.dry_run)

    # Step 3: Execute
    result = executor.run_plan(plan, cfg)
    logger.info(f"[RemediationAgent] Result: success={result.get('success')} error={result.get('error')}")

    return {
        "approved": True,
        "reason":   validation.reason,
        "risk":     validation.risk_level,
        "plan":     {
            "primary_action": plan.primary_action.action,
            "has_fallback":   len(plan.fallback_steps) > 0,
            "dry_run":        plan.dry_run,
        },
        "result": result,
    }
