"""
Verify Node
============
Waits a configurable duration then re-queries CloudWatch to check
if the remediation resolved the incident.

Routing:
  - resolved=True   → END (graph done)
  - resolved=False, retry_count < max_retries → back to execute (try next action)
  - resolved=False, retry_count >= max_retries → escalate

NOTE: Lambda has a 15-minute max timeout. The default verify_wait_seconds=120
means this node sleeps 2 minutes. Keep total Lambda timeout > 5 minutes.
"""

from __future__ import annotations

import logging
import time

import boto3

from aws.config import AgentConfig
from aws.graph.state import AgentState
from aws.tools import cloudwatch

logger = logging.getLogger(__name__)


def verify(state: AgentState) -> dict:
    """
    LangGraph node: wait then re-check CloudWatch to confirm resolution.

    Input state fields used:  resource_name, signal_type, context, action_result, retry_count
    Output state fields set:  resolved, retry_count
    """
    cfg = AgentConfig.load()
    resource = state.get("resource_name", "")
    signal = state.get("signal_type", "")
    context = state.get("context", {})
    retry_count = state.get("retry_count", 0)
    wait = cfg.verify_wait_seconds

    # If execution failed, don't wait — just escalate
    action_result = state.get("action_result") or {}
    if not action_result.get("success"):
        logger.warning(f"[Verify] Action failed — skipping wait, marking unresolved")
        return {"resolved": False, "retry_count": retry_count + 1}

    logger.info(f"[Verify] Waiting {wait}s before checking {resource}...")
    time.sleep(wait)

    resolved = _check_resolved(resource, signal, context, cfg)
    new_retry_count = retry_count + 1

    logger.info(f"[Verify] resolved={resolved} (attempt {new_retry_count}/{cfg.max_retries})")
    return {"resolved": resolved, "retry_count": new_retry_count}


def _check_resolved(
    resource: str,
    signal: str,
    original_context: dict,
    cfg: AgentConfig,
) -> bool:
    """Re-query CloudWatch and compare against original incident thresholds."""
    region = original_context.get("region", cfg.aws_region)
    cw = boto3.client("cloudwatch", region_name=region)
    window = cfg.cw_window_minutes

    if "lambda" in signal or "apigw" in signal:
        return _verify_lambda(cw, resource, original_context, cfg, window)

    if "sqs" in signal or "dlq" in signal:
        return _verify_sqs(cw, resource, cfg, window)

    if "dynamo" in signal:
        return _verify_dynamo(cw, resource, cfg, window)

    # Unknown signal type — can't verify, assume not resolved
    logger.warning(f"[Verify] Unknown signal type '{signal}' — cannot verify, treating as unresolved")
    return False


def _verify_lambda(cw, function_name: str, original: dict, cfg: AgentConfig, window: int) -> bool:
    """Lambda is resolved if error rate dropped below threshold."""
    current_error_rate = cloudwatch.get_lambda_error_rate(cw, function_name, window)
    original_error_rate = original.get("error_rate", 1.0)
    threshold = cfg.error_threshold

    logger.info(
        f"[Verify] Lambda {function_name}: "
        f"original_error_rate={original_error_rate:.1%} "
        f"current_error_rate={current_error_rate:.1%} "
        f"threshold={threshold:.1%}"
    )
    return current_error_rate < threshold


def _verify_sqs(cw, queue_name: str, cfg: AgentConfig, window: int) -> bool:
    """SQS is resolved if DLQ depth dropped below threshold."""
    current_depth = cloudwatch.get_sqs_dlq_depth(cw, queue_name, window)
    threshold = cfg.dlq_threshold
    logger.info(f"[Verify] SQS {queue_name}: depth={current_depth} threshold={threshold}")
    return current_depth < threshold


def _verify_dynamo(cw, table_name: str, cfg: AgentConfig, window: int) -> bool:
    """DynamoDB — alert-only; always 'unresolved' since we don't auto-remediate."""
    throttles = cloudwatch.get_dynamo_throttles(cw, table_name, window)
    total = throttles["read"] + throttles["write"]
    logger.info(f"[Verify] DynamoDB {table_name}: total_throttles={total}")
    return total < cfg.dynamo_threshold
