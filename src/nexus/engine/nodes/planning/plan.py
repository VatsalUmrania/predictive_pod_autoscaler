import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from nexus.engine.tools.registry import get_tool_registry
from nexus.engine.workflow.llm.llm import get_chat_model
from nexus.engine.workflow.state.state import IncidentState

logger = logging.getLogger(__name__)

PLAN_SYSTEM_PROMPT = """You are the NEXUS Autonomous Remediation Planner.
Based on the verified Root Cause Analysis (RCA) and the discovered target workload identity:
1. Select the minimal, safest mutation tool call(s) from your bound tools to remediate the root cause.
2. Populate all required tool arguments strictly according to the tool's schema using the discovered target (`namespace`, `resource_kind`, `resource_name`).
3. If previous remediation attempts failed verification, do not repeat the exact same ineffective tool call—choose an alternative remediation or configuration patch.
4. If no available mutation tool can safely resolve the issue, make zero tool calls and explain why."""


async def plan_node(state: IncidentState) -> dict[str, Any]:
    incident_id = state.get("incident_id", "unknown")
    registry = get_tool_registry()
    mutate_tools = registry.get_tools(mode="mutate", platform=state.get("platform"))

    if not mutate_tools:
        logger.error(
            "Incident %s: No mutation tools registered for platform=%s",
            incident_id,
            state.get("platform"),
        )
        return {
            "remediation_plan": {
                "rationale": "No mutation tools available.",
                "steps": [],
            },
            "status": "UNPLANNABLE",
        }

    llm = get_chat_model(temperature=0.0)
    planner_llm = llm.bind_tools(mutate_tools)

    planning_input = {
        "incident_id": incident_id,
        "discovered_target": {
            "platform": state.get("platform"),
            "namespace": state.get("namespace"),
            "resource_kind": state.get("resource_kind"),
            "service_name": state.get("service_name"),
            "target_resource": state.get("target_resource"),
        },
        "rca_result": state.get("rca_result"),
        "remediation_attempts": state.get("remediation_attempts", 0),
        "previous_execution_results": state.get("execution_results", []),
        "verification_feedback": state.get("verification_feedback"),
    }

    ai_msg = await planner_llm.ainvoke(
        [
            SystemMessage(content=PLAN_SYSTEM_PROMPT),
            HumanMessage(content=json.dumps(planning_input, default=str)),
        ]
    )

    steps: list[dict[str, Any]] = []
    for idx, tc in enumerate(getattr(ai_msg, "tool_calls", None) or [], start=1):
        tool_name = tc["name"]
        tool_args = tc.get("args") or {}
        meta = registry.get_metadata(tool_name)

        steps.append(
            {
                "step": idx,
                "tool_call_id": tc.get("id", f"call_{idx}"),
                "tool_name": tool_name,
                "tool_args": tool_args,
                "blast_radius": getattr(meta, "blast_radius", "medium"),
                "reversible": getattr(meta, "reversible", True),
                "description": f"Execute {tool_name} with {json.dumps(tool_args)}",
            }
        )

    logger.info(
        "Incident %s generated %d schema-bound remediation step(s).",
        incident_id,
        len(steps),
    )
    return {
        "remediation_plan": {
            "rationale": ai_msg.content or state.get("suspected_cause", ""),
            "steps": steps,
        },
        "status": "PLANNED" if steps else "UNPLANNABLE",
    }
