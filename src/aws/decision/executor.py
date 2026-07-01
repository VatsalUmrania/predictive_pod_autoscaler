"""
Phase 6 — Decision Engine: Executor
======================================
Calls boto3 tools based on the execution plan.

This is the ONLY place in the codebase that makes AWS write calls.
The LLM never reaches here directly — it goes through:

  LLM → validator → planner → executor → AWS SDK

Each action maps to a tool function that returns a result dict.
"""

from __future__ import annotations

import logging
from typing import Any

import boto3

from aws.config import AgentConfig
from aws.decision.planner import ActionStep, ExecutionPlan
from aws.tools import lambda_tools, sqs_tools

logger = logging.getLogger(__name__)


def run_plan(plan: ExecutionPlan, cfg: AgentConfig | None = None) -> dict[str, Any]:
    """
    Execute the primary action in the plan.

    If primary fails and there are fallbacks, tries the first fallback.
    Returns a result dict with success/failure info.
    """
    cfg = cfg or AgentConfig.load()
    region = cfg.aws_region
    dry_run = plan.dry_run or cfg.dry_run

    # Create boto3 clients
    lambda_client = boto3.client("lambda", region_name=region)
    sqs_client    = boto3.client("sqs",    region_name=region)

    # Try primary action
    result = _run_step(plan.primary_action, lambda_client, sqs_client, dry_run)

    # If primary failed and there are fallbacks, try the first one
    if not result.get("success") and plan.fallback_steps:
        logger.warning(f"[Executor] Primary action failed — trying fallback")
        fallback = plan.fallback_steps[0]
        fallback_result = _run_step(fallback, lambda_client, sqs_client, dry_run)
        result["fallback_attempted"] = True
        result["fallback_result"] = fallback_result

    result["action"] = plan.primary_action.action
    result["resource"] = plan.resource_name
    result["dry_run"] = dry_run
    return result


def run_step(step: ActionStep, cfg: AgentConfig | None = None) -> dict[str, Any]:
    """Execute a single action step (used for fallback/rollback)."""
    cfg = cfg or AgentConfig.load()
    lambda_client = boto3.client("lambda", region_name=cfg.aws_region)
    sqs_client    = boto3.client("sqs",    region_name=cfg.aws_region)
    return _run_step(step, lambda_client, sqs_client, cfg.dry_run)


# ── Internal dispatch ─────────────────────────────────────────────────────────

def _run_step(
    step: ActionStep,
    lambda_client,
    sqs_client,
    dry_run: bool,
) -> dict[str, Any]:
    action = step.action
    params = step.params
    resource = params.get("function_name", "")

    logger.info(f"[Executor] action={action} resource={resource} dry_run={dry_run}")

    if action == "alert_only":
        return {"success": True, "action": "alert_only", "pre": {}, "post": {}, "error": None}

    elif action == "increase_memory":
        return lambda_tools.increase_memory(
            lambda_client, resource, int(params.get("memory_mb", 512)), dry_run=dry_run
        )

    elif action == "increase_timeout":
        return lambda_tools.increase_timeout(
            lambda_client, resource, int(params.get("timeout_seconds", 30)), dry_run=dry_run
        )

    elif action == "rollback_alias":
        return lambda_tools.rollback_alias(
            lambda_client, resource,
            alias_name=params.get("alias_name", "live"),
            target_version=params.get("target_version"),
            dry_run=dry_run,
        )

    elif action == "set_concurrency":
        return lambda_tools.set_reserved_concurrency(
            lambda_client, resource,
            int(params.get("reserved_concurrent_executions", 100)),
            dry_run=dry_run,
        )

    elif action == "replay_dlq":
        dlq_url = params.get("dlq_url", "")
        source_url = params.get("source_queue_url", "")
        if not dlq_url or not source_url:
            return {
                "success": False,
                "error": "replay_dlq requires dlq_url and source_queue_url in params",
                "pre": {}, "post": {},
            }
        return sqs_tools.replay_dlq(sqs_client, dlq_url, source_url, dry_run=dry_run)

    else:
        return {
            "success": False,
            "error": f"Unknown action '{action}'",
            "pre": {}, "post": {},
        }
