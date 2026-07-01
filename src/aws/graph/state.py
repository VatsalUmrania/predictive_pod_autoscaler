"""
Agent State
============
The AgentState TypedDict is the single shared data structure
that flows through every node in the LangGraph workflow.

Each node reads from state and returns a dict with only the fields it updates.
LangGraph merges these updates automatically.

Field lifecycle:
  - resource_name, signal_type, raw_metrics  →  set by handler.py (graph input)
  - context                                  →  set by collect node
  - rca                                      →  set by diagnose node
  - approved                                 →  set by policy node
  - action_result                            →  set by execute node
  - resolved, retry_count                    →  set by verify node
  - escalated                                →  set by escalate node
"""

from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    """
    Shared state passed between all LangGraph nodes.

    ``total=False`` means all fields are optional — nodes only update
    the fields they are responsible for.
    """

    # ── Input (set by handler before graph invocation) ───────────────────────
    resource_name:  str     # e.g. "payment-api-lambda"
    signal_type:    str     # e.g. "lambda_error_rate_high"
    raw_metrics:    dict    # Raw metric values from CloudWatch (pre-collected)
    region:         str     # AWS region, e.g. "us-east-1"

    # ── Collect node output ───────────────────────────────────────────────────
    context: dict           # Rich context dict:
                            #   {
                            #     "error_rate": 0.15,
                            #     "throttle_count": 0,
                            #     "duration_p99_ms": 2800,
                            #     "timeout_ms": 3000,
                            #     "memory_mb": 256,
                            #     "recent_errors": ["...log lines..."],
                            #     "function_versions": [...],
                            #     "dlq_depth": 0,
                            #     "dlq_samples": [...],
                            #   }

    # ── Diagnose node output ──────────────────────────────────────────────────
    rca: dict | None        # Claude's structured analysis:
                            #   {
                            #     "root_cause": "Lambda OOM — process killed by runtime",
                            #     "failure_class": "resource_exhaustion",
                            #     "action": "increase_memory",
                            #     "params": {"memory_mb": 512},
                            #     "confidence": 0.91,
                            #     "reasoning": "...",
                            #   }

    # ── Policy node output ────────────────────────────────────────────────────
    approved:       bool    # True if auto-execute; False → escalate for approval
    policy_reason:  str     # Explanation of policy decision

    # ── Execute node output ───────────────────────────────────────────────────
    action_result: dict | None  # Result from tool function:
                                #   {"success": bool, "pre": {}, "post": {}, "error": str|None}

    # ── Verify node output ────────────────────────────────────────────────────
    resolved:       bool    # True if post-remediation metrics are healthy
    retry_count:    int     # Number of execute→verify cycles attempted

    # ── Escalate node output ──────────────────────────────────────────────────
    escalated:      bool    # True if human was notified
    slack_sent:     bool    # True if Slack notification was delivered
