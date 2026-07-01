"""
LangGraph Workflow
===================
Compiles the incident response graph and exposes run_graph() as the
single entry point for the Lambda handler.

Graph topology:

  START
    │
  collect           ← gather CloudWatch + logs + config
    │
  diagnose          ← Claude Sonnet → structured RCA JSON
    │
  policy            ← whitelist + confidence threshold
    │
  ┌─────────────────┤
  │ approved?       │
  │                 │
YES                NO
  │                 │
execute          escalate (approval request) → END
  │
verify
  │
  ├── resolved=True  → notify_resolved → END
  │
  ├── resolved=False, retry < max  → execute (retry)
  │
  └── resolved=False, retry >= max → escalate (failed) → END

Usage:
    from aws.graph.workflow import run_graph

    result = run_graph({
        "resource_name": "payment-api",
        "signal_type":   "lambda_error_rate_high",
        "raw_metrics":   {"error_rate": 0.15},
        "region":        "us-east-1",
    })
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from langgraph.graph import END, StateGraph

from aws.config import AgentConfig
from aws.graph.state import AgentState
from aws.graph.nodes.collect   import collect
from aws.graph.nodes.diagnose  import diagnose
from aws.graph.nodes.policy    import policy
from aws.graph.nodes.execute   import execute
from aws.graph.nodes.verify    import verify
from aws.graph.nodes.escalate  import escalate, notify_resolved

logger = logging.getLogger(__name__)


# ── Routing functions (conditional edges) ────────────────────────────────────

def route_after_policy(state: AgentState) -> Literal["execute", "escalate"]:
    """Route based on whether the policy node approved the action."""
    if state.get("approved"):
        return "execute"
    return "escalate"


def route_after_verify(state: AgentState) -> Literal["notify_resolved", "execute", "escalate"]:
    """
    Route based on verification result:
      - resolved   → notify_resolved (success path)
      - not resolved, retries remaining → execute (retry)
      - not resolved, max retries hit   → escalate (failure path)
    """
    cfg = AgentConfig.load()
    resolved = state.get("resolved", False)
    retry_count = state.get("retry_count", 0)

    if resolved:
        return "notify_resolved"

    if retry_count < cfg.max_retries:
        logger.info(f"[Workflow] Verification failed — retrying (attempt {retry_count + 1}/{cfg.max_retries})")
        return "execute"

    logger.warning(f"[Workflow] Max retries ({cfg.max_retries}) reached — escalating")
    return "escalate"


# ── Build graph ───────────────────────────────────────────────────────────────

def _build_graph() -> StateGraph:
    graph = StateGraph(AgentState)

    # Add all nodes
    graph.add_node("collect",         collect)
    graph.add_node("diagnose",        diagnose)
    graph.add_node("policy",          policy)
    graph.add_node("execute",         execute)
    graph.add_node("verify",          verify)
    graph.add_node("escalate",        escalate)
    graph.add_node("notify_resolved", notify_resolved)

    # Linear edges
    graph.set_entry_point("collect")
    graph.add_edge("collect",  "diagnose")
    graph.add_edge("diagnose", "policy")

    # Conditional: policy → execute or escalate
    graph.add_conditional_edges(
        "policy",
        route_after_policy,
        {"execute": "execute", "escalate": "escalate"},
    )

    # execute → verify (always)
    graph.add_edge("execute", "verify")

    # Conditional: verify → notify_resolved, execute (retry), or escalate
    graph.add_conditional_edges(
        "verify",
        route_after_verify,
        {
            "notify_resolved": "notify_resolved",
            "execute":         "execute",
            "escalate":        "escalate",
        },
    )

    # Terminal nodes
    graph.add_edge("notify_resolved", END)
    graph.add_edge("escalate",        END)

    return graph


# Compile once at module load (Lambda cold start) — not per-invocation
_compiled_graph = _build_graph().compile()


# ── Public entrypoint ─────────────────────────────────────────────────────────

def run_graph(incident: dict[str, Any]) -> dict[str, Any]:
    """
    Run the full incident response pipeline for one alarm event.

    Args:
        incident: dict with keys:
            resource_name  (str)   — e.g. "payment-api"
            signal_type    (str)   — e.g. "lambda_error_rate_high"
            raw_metrics    (dict)  — optional pre-collected metric data
            region         (str)   — AWS region (default: us-east-1)

    Returns:
        Final AgentState dict with all fields populated.
    """
    # Provide defaults for optional fields
    initial_state: AgentState = {
        "resource_name": incident.get("resource_name", "unknown"),
        "signal_type":   incident.get("signal_type", "unknown"),
        "raw_metrics":   incident.get("raw_metrics", {}),
        "region":        incident.get("region", "us-east-1"),
        "retry_count":   0,
        "resolved":      False,
        "approved":      False,
        "escalated":     False,
        "slack_sent":    False,
    }

    logger.info(
        f"[Workflow] Starting graph for "
        f"{initial_state['resource_name']} / {initial_state['signal_type']}"
    )

    try:
        final_state = _compiled_graph.invoke(initial_state)
    except Exception as exc:
        logger.error(f"[Workflow] Graph execution failed: {exc}", exc_info=True)
        final_state = {**initial_state, "resolved": False, "escalated": True}

    logger.info(
        f"[Workflow] Done: resolved={final_state.get('resolved')} "
        f"escalated={final_state.get('escalated')} "
        f"action={final_state.get('rca', {}).get('action', 'none')}"
    )
    return final_state
