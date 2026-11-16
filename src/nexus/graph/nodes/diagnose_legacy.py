"""
NEXUS Legacy Diagnostic Node
============================
Backward-compatible single-node ReAct investigation and Reflexion execution.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import HumanMessage

from nexus.engine.fsm import IncidentState
from nexus.graph.nodes import diagnose as diagnose_mod
from nexus.graph.nodes.diagnose_helpers import (
    get_hardened_diagnostic_tools,
    parse_rca_json,
)
from nexus.graph.nodes.diagnose_reflexion import run_diagnose_reflexion
from nexus.graph.rca import RCALeadSynthesizer
from nexus.reasoning.rca_validator import RCAValidator, downgrade_rca

logger = logging.getLogger(__name__)


async def diagnose_node(state: dict[str, Any]) -> dict[str, Any]:
    if state.get("fallback_active"):
        return {"fallback_active": True, "fsm_state": IncidentState.DIAGNOSING.value}
    cluster = None
    try:
        from nexus.graph.nodes.diagnose_cluster import build_diagnose_cluster

        cluster = build_diagnose_cluster(
            state.get("incident_id", "inc"),
            state.get("events", []),
            state.get("telemetry", {}),
            state.get("target", {}),
        )
    except Exception:
        pass
    rca_cls = diagnose_mod.RCAEngine
    rca_engine = rca_cls()
    if (
        cluster
        and type(rca_engine) is not RCALeadSynthesizer
        and hasattr(rca_engine, "analyze")
    ):
        import inspect

        res = rca_engine.analyze(cluster)
        rca_res = await res if inspect.isawaitable(res) else res
        val = RCAValidator()
        verd = val.validate(cluster, rca_res)
        refl: list[dict[str, Any]] = []
        final_rca = rca_res
        if not verd.passed and hasattr(rca_engine, "refine"):
            final_rca, verd, refl = await run_diagnose_reflexion(
                cluster, rca_engine, rca_res, val, verd
            )
        elif not verd.passed:
            final_rca = downgrade_rca(
                rca_res, reason=f"Blocked: {verd.block_reason}", cluster=cluster
            )
        diag = {
            "root_cause": final_rca.root_cause,
            "failure_class": final_rca.failure_class,
            "confidence": final_rca.confidence,
            "suggested_action": final_rca.suggested_action,
            "validator_passed": verd.passed,
        }
        return {
            "diagnosis": diag,
            "diagnostic_reflections": refl,
            "fsm_state": IncidentState.PLANNING.value,
        }
    now = datetime.now(timezone.utc).isoformat()
    try:
        make_agent = diagnose_mod.create_react_agent
        model = diagnose_mod.get_llm()
        agent = make_agent(model, get_hardened_diagnostic_tools())
        outcome = await asyncio.wait_for(
            agent.ainvoke({"messages": [HumanMessage(content="Investigate")]}),
            timeout=60.0,
        )
        parsed = parse_rca_json(str(outcome.get("messages", [None])[-1].content))
        if not parsed:
            raise ValueError("Invalid RCA JSON")
        diag = {
            "root_cause": parsed.get("root_cause", "RCA"),
            "failure_class": parsed.get("failure_class", "resource_exhaustion"),
            "confidence": float(parsed.get("confidence", 0.9)),
            "suggested_action": parsed.get(
                "suggested_action", "k8s_restart_deployment"
            ),
        }
        audit_entry = {
            "timestamp": now,
            "stage": "diagnose",
            "action": "react_investigation_complete",
            "details": f"Root Cause: {diag.get('root_cause')}",
        }
        return {
            "diagnosis": diag,
            "agentic_rca": diag,
            "fallback_active": False,
            "fsm_state": IncidentState.PLANNING.value,
            "messages": outcome.get("messages", []),
            "audit_log": [audit_entry],
        }
    except Exception as exc:
        return {
            "fallback_active": True,
            "fsm_state": IncidentState.DIAGNOSING.value,
            "audit_log": [
                {
                    "timestamp": now,
                    "stage": "diagnose",
                    "action": "react_fallback_activated",
                    "details": f"{type(exc).__name__}: {exc}",
                }
            ],
        }
