"""
Collect Node (LangGraph)
=========================
Thin orchestration wrapper — delegates to collector.context_builder.

Phase 4: The context builder assembles everything the LLM needs:
  metrics + logs + deployment history + CloudTrail + X-Ray + dependencies
"""

from __future__ import annotations

import logging

from aws.config import AgentConfig
from aws.graph.state import AgentState
from aws.collector.context_builder import build_context

logger = logging.getLogger(__name__)


def collect(state: AgentState) -> dict:
    """
    LangGraph node: build rich incident context using the collector module.

    Input fields:  resource_name, signal_type, raw_metrics, region
    Output fields: context
    """
    cfg = AgentConfig.load()
    resource = state["resource_name"]
    signal   = state["signal_type"]
    region   = state.get("region", cfg.aws_region)
    alarm_name   = state.get("raw_metrics", {}).get("alarm_name")
    alarm_reason = state.get("raw_metrics", {}).get("alarm_reason", "")

    logger.info(f"[Collect] Gathering context for {resource} ({signal})")

    context = build_context(
        resource_name=resource,
        signal_type=signal,
        region=region,
        alarm_name=alarm_name,
        alarm_reason=alarm_reason,
        cfg=cfg,
    )

    return {"context": context}
