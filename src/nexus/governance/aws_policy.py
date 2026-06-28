"""
NEXUS AWS Action Policy
========================
Defines the whitelist of permitted AWS remediation actions and their
blast-radius constraints. The RunbookExecutor validates every AWS action
against this policy before executing.

Safety principles:
    • Every action has a maximum healing_level — the governance ladder
      will never execute above the declared level autonomously.
    • Actions with requires_human_approval=True are always staged in
      the HumanApprovalQueue regardless of confidence.
    • Parameter bounds prevent the LLM from requesting unbounded changes
      (e.g., setting Lambda memory to 100 GB).
    • All actions are idempotent where possible.

Policy enforcement is layered:
    1. AWSPolicy.check()     — called by AWSTools before any boto3 call
    2. ActionLadder.evaluate() — existing governance ladder (unchanged)
    3. AuditTrail             — every decision logged before execution
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AWSActionPolicy:
    """
    Policy spec for a single AWS remediation action type.

    Attributes:
        action_type:            Unique key matching RunbookAction.type
        healing_level:          0-3, maps to the Action Ladder
        requires_human_approval: If True, never executes autonomously
        max_params:             Upper-bound constraints on numeric params
        description:            Human-readable policy summary
    """
    action_type: str
    healing_level: int
    requires_human_approval: bool = False
    max_params: dict[str, Any] = field(default_factory=dict)
    description: str = ""


# ── Master AWS Action Whitelist ───────────────────────────────────────────────
#
# Only actions listed here can be executed by the AWSTools layer.
# Any action type NOT in this whitelist is rejected with a PolicyViolation.
#
AWS_POLICY: dict[str, AWSActionPolicy] = {

    # ── Read-only / observability ──────────────────────────────────────────────
    "get_lambda_config": AWSActionPolicy(
        action_type="get_lambda_config",
        healing_level=0,
        description="Fetch Lambda function configuration (read-only)",
    ),
    "get_cloudwatch_logs": AWSActionPolicy(
        action_type="get_cloudwatch_logs",
        healing_level=0,
        description="Fetch recent CloudWatch log lines (read-only)",
    ),
    "get_sqs_attributes": AWSActionPolicy(
        action_type="get_sqs_attributes",
        healing_level=0,
        description="Fetch SQS queue attributes (read-only)",
    ),
    "emit_alert": AWSActionPolicy(
        action_type="emit_alert",
        healing_level=0,
        description="Publish an alert notification via Notifier (read-only)",
    ),

    # ── L1 — No-regret ────────────────────────────────────────────────────────
    "enable_eventbridge_rule": AWSActionPolicy(
        action_type="enable_eventbridge_rule",
        healing_level=1,
        description="Re-enable a previously disabled EventBridge rule",
    ),
    "tag_lambda_function": AWSActionPolicy(
        action_type="tag_lambda_function",
        healing_level=1,
        description="Add NEXUS diagnostic tags to a Lambda function",
    ),

    # ── L2 — Bounded mitigation ───────────────────────────────────────────────
    "increase_lambda_memory": AWSActionPolicy(
        action_type="increase_lambda_memory",
        healing_level=2,
        max_params={"memory_mb": 3008},  # AWS Lambda max
        description="Increase Lambda function memory (max 3008 MB)",
    ),
    "increase_lambda_timeout": AWSActionPolicy(
        action_type="increase_lambda_timeout",
        healing_level=2,
        max_params={"timeout_seconds": 900},  # AWS Lambda max
        description="Increase Lambda function timeout (max 900s)",
    ),
    "set_lambda_reserved_concurrency": AWSActionPolicy(
        action_type="set_lambda_reserved_concurrency",
        healing_level=2,
        max_params={"reserved_concurrent_executions": 1000},
        description="Set Lambda reserved concurrency limit",
    ),
    "replay_sqs_dlq": AWSActionPolicy(
        action_type="replay_sqs_dlq",
        healing_level=2,
        max_params={"max_messages": 1000},
        description="Move messages from DLQ back to the source queue (max 1000)",
    ),
    "disable_eventbridge_rule": AWSActionPolicy(
        action_type="disable_eventbridge_rule",
        healing_level=2,
        description="Disable an EventBridge rule causing runaway Lambda invocations",
    ),
    "update_lambda_env_var": AWSActionPolicy(
        action_type="update_lambda_env_var",
        healing_level=2,
        description="Patch a single environment variable on a Lambda function",
    ),

    # ── L3 — Significant change (human approval required below 0.85 confidence)
    "rollback_lambda_alias": AWSActionPolicy(
        action_type="rollback_lambda_alias",
        healing_level=3,
        description="Roll back a Lambda alias to the previous published version",
    ),
    "purge_sqs_queue": AWSActionPolicy(
        action_type="purge_sqs_queue",
        healing_level=3,
        requires_human_approval=True,    # Always requires human approval — destructive
        description="Purge all messages from an SQS queue (DESTRUCTIVE — always human-approved)",
    ),
}


class PolicyViolation(Exception):
    """Raised when an action is blocked by the AWS policy."""
    pass


def check_action(action_type: str, params: dict[str, Any]) -> AWSActionPolicy:
    """
    Validate an action against the AWS policy whitelist.

    Returns the matching AWSActionPolicy if allowed.
    Raises PolicyViolation if:
        - action_type is not in the whitelist, or
        - a numeric param exceeds its max_params bound.
    """
    policy = AWS_POLICY.get(action_type)
    if policy is None:
        raise PolicyViolation(
            f"Action '{action_type}' is not in the AWS action whitelist. "
            f"Allowed: {sorted(AWS_POLICY.keys())}"
        )

    # Check parameter bounds
    for param_key, max_value in policy.max_params.items():
        param_value = params.get(param_key)
        if param_value is not None:
            try:
                if float(param_value) > float(max_value):
                    raise PolicyViolation(
                        f"Action '{action_type}': param '{param_key}' value {param_value} "
                        f"exceeds policy maximum {max_value}"
                    )
            except (TypeError, ValueError):
                pass  # Non-numeric params — skip bound check

    logger.debug(f"[AWSPolicy] Allowed: {action_type} params={params}")
    return policy
