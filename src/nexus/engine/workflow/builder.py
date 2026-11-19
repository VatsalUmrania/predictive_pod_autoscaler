"""
NEXUS LangGraph StateGraph Builder with Native Fault Tolerance
==============================================================
Uses LangGraph's built-in RetryPolicy, TimeoutPolicy, and node-level
error_handler for transient LLM/API failures (e.g. 504 DEADLINE_EXCEEDED)
and graceful escalation on retry exhaustion.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.errors import NodeTimeoutError
from langgraph.graph import END, StateGraph
from langgraph.types import Command, RetryPolicy, TimeoutPolicy, default_retry_on

from nexus.engine.nodes.approval import approval_node
from nexus.engine.nodes.diagnose import diagnose_node
from nexus.engine.nodes.escalate import escalate_node
from nexus.engine.nodes.execute import execute_node
from nexus.engine.nodes.govern import govern_node
from nexus.engine.nodes.learn import learn_node
from nexus.engine.nodes.plan import plan_node
from nexus.engine.nodes.rollback import rollback_node
from nexus.engine.nodes.triage import triage_node
from nexus.engine.nodes.verify import verify_node
from nexus.engine.workflow.llm_limiter import LLMQuotaExhaustedError
from nexus.engine.workflow.routes import (
    route_after_approval,
    route_after_diagnose,
    route_after_execute,
    route_after_govern,
    route_after_plan,
    route_after_triage,
    route_after_verify,
)
from nexus.engine.workflow.state import IncidentState

logger = logging.getLogger(__name__)


def is_retryable_node_error(exc: Exception) -> bool:
    """
    Predicate for LangGraph RetryPolicy:
      - Fast-fail on 429 quota exhaustion (LLMQuotaExhaustedError).
      - Retry on TimeoutError / NodeTimeoutError and LangGraph's default_retry_on
        (ConnectionError, 5xx HTTP errors, GoogleAPIError 504 DEADLINE_EXCEEDED, etc.).
    """
    if isinstance(exc, LLMQuotaExhaustedError):
        return False
    if isinstance(exc, (TimeoutError, NodeTimeoutError)):
        return True
    return default_retry_on(exc)


def _build_llm_retry_policy() -> RetryPolicy:
    return RetryPolicy(
        max_attempts=int(os.getenv("NEXUS_NODE_MAX_ATTEMPTS", "3")),
        initial_interval=float(os.getenv("NEXUS_NODE_RETRY_INITIAL_S", "1.0")),
        backoff_factor=2.0,
        max_interval=10.0,
        jitter=True,
        retry_on=is_retryable_node_error,
    )


def _build_llm_timeout_policy() -> TimeoutPolicy:
    return TimeoutPolicy(
        run_timeout=float(os.getenv("NEXUS_NODE_RUN_TIMEOUT_S", "120.0")),
        idle_timeout=float(os.getenv("NEXUS_NODE_IDLE_TIMEOUT_S", "60.0")),
    )


async def _escalate_on_node_error(
    state: IncidentState, config: RunnableConfig
) -> Command[Literal["escalate"]]:
    """
    LangGraph node-level error_handler: when retries are exhausted or a non-retryable
    error (e.g. 429 quota exhaustion) occurs, route gracefully to `escalate` instead
    of crashing the workflow.
    """
    node_err = (config.get("configurable") or {}).get("__pregel_node_error")
    if node_err is not None:
        err_msg = f"Node '{node_err.node}' failed after retries: {node_err.error}"
    else:
        err_msg = "Workflow node failed after exhausting retries"

    logger.error(
        "Incident %s routing to escalate via LangGraph error_handler: %s",
        state.get("incident_id", "unknown"),
        err_msg,
    )
    return Command(
        goto="escalate",
        update={"status": "ESCALATED", "error": err_msg},
    )


_FAULT_TOLERANT_LLM_NODES = frozenset({"diagnose", "plan", "verify"})
_FAULT_TOLERANT_POLICY_NODES = frozenset({"govern"})


def build_incident_graph(
    checkpointer: Any | None = None,
    nodes: dict[str, Any] | None = None,
    *,
    retry_policy: RetryPolicy | None = None,
    timeout_policy: TimeoutPolicy | None = None,
) -> Any:
    workflow = StateGraph(IncidentState)

    node_map: dict[str, Any] = {
        "triage": triage_node,
        "diagnose": diagnose_node,
        "plan": plan_node,
        "govern": govern_node,
        "approval": approval_node,
        "execute": execute_node,
        "verify": verify_node,
        "rollback": rollback_node,
        "learn": learn_node,
        "escalate": escalate_node,
    }
    if nodes:
        node_map.update(nodes)

    llm_retry = retry_policy or _build_llm_retry_policy()
    llm_timeout = timeout_policy or _build_llm_timeout_policy()
    policy_retry = RetryPolicy(
        max_attempts=2,
        initial_interval=0.5,
        backoff_factor=2.0,
        jitter=True,
        retry_on=is_retryable_node_error,
    )

    for name, fn in node_map.items():
        if name in _FAULT_TOLERANT_LLM_NODES:
            workflow.add_node(
                name,
                fn,
                retry_policy=llm_retry,
                timeout=llm_timeout,
                error_handler=_escalate_on_node_error,
            )
        elif name in _FAULT_TOLERANT_POLICY_NODES:
            workflow.add_node(
                name,
                fn,
                retry_policy=policy_retry,
                error_handler=_escalate_on_node_error,
            )
        else:
            workflow.add_node(name, fn)

    workflow.set_entry_point("triage")

    workflow.add_conditional_edges(
        "triage", route_after_triage, {"diagnose": "diagnose", "__end__": END}
    )
    workflow.add_conditional_edges(
        "diagnose",
        route_after_diagnose,
        {"plan": "plan", "escalate": "escalate"},
    )
    workflow.add_conditional_edges(
        "plan", route_after_plan, {"govern": "govern", "escalate": "escalate"}
    )
    workflow.add_conditional_edges(
        "govern",
        route_after_govern,
        {"approval": "approval", "execute": "execute", "escalate": "escalate"},
    )
    workflow.add_conditional_edges(
        "approval",
        route_after_approval,
        {"execute": "execute", "escalate": "escalate"},
    )
    workflow.add_conditional_edges(
        "execute",
        route_after_execute,
        {"verify": "verify", "rollback": "rollback", "escalate": "escalate"},
    )
    workflow.add_conditional_edges(
        "verify",
        route_after_verify,
        {
            "learn": "learn",
            "diagnose": "diagnose",
            "rollback": "rollback",
            "escalate": "escalate",
        },
    )

    workflow.add_edge("rollback", "escalate")
    workflow.add_edge("learn", END)
    workflow.add_edge("escalate", END)

    return workflow.compile(checkpointer=checkpointer)


def build_nexus_agent_graph(
    nodes: dict[str, Any] | None = None, checkpointer: Any | None = None
) -> Any:
    """Compatibility alias for build_incident_graph."""
    return build_incident_graph(checkpointer=checkpointer, nodes=nodes)
