"""
Diagnose Node (LangGraph)
==========================
Thin wrapper — delegates to agents.diagnosis_agent.
Also injects memory (past incidents) into the context before calling Claude.

Phase 5 + 9: AI Diagnosis + Memory injection
"""

from __future__ import annotations

import logging

from aws.config import AgentConfig
from aws.graph.state import AgentState
from aws.agents.diagnosis_agent import diagnose as _diagnose
from aws.memory.incidents import get_recent_incidents, format_memory_for_prompt

logger = logging.getLogger(__name__)


def diagnose(state: AgentState) -> dict:
    """
    LangGraph node: inject memory context, then call Claude Sonnet for RCA.

    Input fields:  context
    Output fields: rca
    """
    cfg = AgentConfig.load()
    context = state.get("context", {})
    resource = state.get("resource_name", "unknown")

    # Phase 9: inject past incidents into context before calling LLM
    past_incidents = get_recent_incidents(resource, limit=5, region=cfg.aws_region)
    if past_incidents:
        context["memory"] = format_memory_for_prompt(past_incidents)
        logger.info(f"[Diagnose] Injecting {len(past_incidents)} past incidents into context")

    rca = _diagnose(context, cfg)
    return {"rca": rca}
