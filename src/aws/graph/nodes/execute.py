"""
Execute Node
=============
Calls the appropriate boto3 tool based on the RCA recommendation.

This is the only node that actually modifies AWS resources.
It only runs when policy.py has set approved=True.

Each action maps to a tool function that:
  - Validates bounds internally
  - Returns a result dict (success, pre, post, error)
  - Never raises

After execution, the result is stored in state["action_result"]
for the verify node to use.
"""

from __future__ import annotations

import logging

import boto3

from aws.config import AgentConfig
from aws.graph.state import AgentState
from aws.tools import lambda_tools, sqs_tools

logger = logging.getLogger(__name__)


def execute(state: AgentState) -> dict:
    """
    LangGraph node: execute the approved remediation action via boto3.

    Input state fields used:  rca, context, region
    Output state fields set:  action_result
    """
    cfg = AgentConfig.load()
    rca = state.get("rca") or {}
    action = rca.get("action", "alert_only")
    params = rca.get("params", {})
    resource = state.get("resource_name", "")
    region = state.get("region", cfg.aws_region)
    dry_run = cfg.dry_run

    logger.info(f"[Execute] action={action} resource={resource} dry_run={dry_run}")

    if action == "alert_only":
        # Nothing to execute — escalate node handles the notification
        logger.info("[Execute] alert_only — no AWS changes made")
        return {"action_result": {"success": True, "action": "alert_only", "pre": {}, "post": {}, "error": None}}

    # Create boto3 clients
    lambda_ = boto3.client("lambda", region_name=region)
    sqs     = boto3.client("sqs",    region_name=region)

    result = _dispatch(action, params, resource, lambda_, sqs, dry_run)
    result["action"] = action
    logger.info(f"[Execute] Result: success={result['success']} error={result.get('error')}")
    return {"action_result": result}


def _dispatch(
    action: str,
    params: dict,
    resource: str,
    lambda_client,
    sqs_client,
    dry_run: bool,
) -> dict:
    """Route action to the correct tool function."""

    if action == "increase_memory":
        # Calculate 1.5x if LLM didn't specify — it usually will, but be safe
        current_memory = int(params.get("memory_mb", 512))
        return lambda_tools.increase_memory(
            lambda_client,
            function_name=params.get("function_name", resource),
            memory_mb=current_memory,
            dry_run=dry_run,
        )

    elif action == "increase_timeout":
        current_timeout = int(params.get("timeout_seconds", 30))
        return lambda_tools.increase_timeout(
            lambda_client,
            function_name=params.get("function_name", resource),
            timeout_seconds=current_timeout,
            dry_run=dry_run,
        )

    elif action == "rollback_alias":
        return lambda_tools.rollback_alias(
            lambda_client,
            function_name=params.get("function_name", resource),
            alias_name=params.get("alias_name", "live"),
            target_version=params.get("target_version"),   # None = auto-detect
            dry_run=dry_run,
        )

    elif action == "set_concurrency":
        reserved = int(params.get("reserved_concurrent_executions", 100))
        return lambda_tools.set_reserved_concurrency(
            lambda_client,
            function_name=params.get("function_name", resource),
            reserved_concurrent_executions=reserved,
            dry_run=dry_run,
        )

    elif action == "replay_dlq":
        dlq_url = params.get("dlq_url") or state_dlq_url(resource)
        source_url = params.get("source_queue_url", "")
        if not dlq_url or not source_url:
            return {
                "success": False,
                "error": "replay_dlq requires dlq_url and source_queue_url in params",
                "pre": {}, "post": {},
            }
        return sqs_tools.replay_dlq(
            sqs_client,
            dlq_url=dlq_url,
            source_queue_url=source_url,
            max_messages=100,
            dry_run=dry_run,
        )

    else:
        return {
            "success": False,
            "error": f"Unknown action '{action}' — this should have been caught by policy node",
            "pre": {}, "post": {},
        }


def state_dlq_url(resource: str) -> str | None:
    """Fallback: try to construct DLQ URL from resource name convention."""
    # Convention: if resource ends in '-dlq' or '-DLQ', treat as DLQ URL hint
    # Real DLQ URL should come from LLM params or context
    return None
