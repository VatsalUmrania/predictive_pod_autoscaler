import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from nexus.graph.llm import get_chat_model
from nexus.graph.nodes.diagnose_builders import truncate_observation
from nexus.graph.rca.result import RCAResult
from nexus.graph.state import IncidentState
from nexus.graph.workflow_state import sync_discovered_target_identity
from nexus.tools.registry import get_tool_registry

logger = logging.getLogger(__name__)

DIAGNOSE_SYSTEM_PROMPT = """You are the NEXUS Autonomous SRE Investigator.
You receive raw telemetry and symptom events where the namespace or owning service may be missing (None) or only a Pod/instance name is mentioned.

Follow this exact investigation protocol using your read-only observation tools:
1. LOCATE THE RESOURCE: If `known_namespace` is None, use your cluster/cloud listing or search tools across namespaces to find the involved Pod or resource.
2. TRACE OWNERSHIP: Inspect the resource's metadata and `ownerReferences` (e.g., Pod -> ReplicaSet -> Deployment or StatefulSet) to identify the true owning controller (`resource_kind` and `resource_name`) and its exact `namespace_or_region`. Never guess or report 'unknown-service' if a tool can inspect the cluster.
3. DIAGNOSE ROOT CAUSE: Inspect container statuses, exit codes, readiness/liveness probe configurations, container commands/args, resource limits, recent events, and logs.
4. ASSESS AUTO-REMEDIATION & FIX STRATEGY:
   - Check `available_mutation_tools` to see what remediation tools the planner has available.
   - Set `can_auto_remediate = True` whenever an available mutation tool can fix or mitigate the root cause (for example: patching misconfigured readiness/liveness probes or too-short `initialDelaySeconds`/timeouts, adjusting resource limits, fixing env/config fields, scaling replicas, or restarting/rolling back a broken rollout).
   - Set `can_auto_remediate = False` only if no available mutation tool can fix the root cause (e.g., application source-code bug or external third-party outage).
   - ALWAYS populate `recommended_strategy` with a concrete, actionable fix (exact resource, spec fields, or operator commands), even when `can_auto_remediate` is `False`.
5. SYNTHESIZE: Once you have verified evidence from your tool calls, stop calling tools and summarize your findings concisely."""


async def diagnose_node(state: IncidentState) -> dict[str, Any]:
    incident_id = state.get("incident_id", "unknown")
    registry = get_tool_registry()
    observe_tools = registry.get_tools(mode="observe", platform=state.get("platform"))
    mutate_tools = registry.get_tools(mode="mutate", platform=state.get("platform"))
    tool_map = {t.name: t for t in observe_tools}
    available_mutation_tools = [
        {
            "name": t.name,
            "description": (getattr(t, "description", "") or "").splitlines()[0][:160],
        }
        for t in mutate_tools
    ]

    llm = get_chat_model(temperature=0.0)
    llm_with_tools = llm.bind_tools(observe_tools) if observe_tools else llm

    context_payload = {
        "incident_id": incident_id,
        "known_platform": state.get("platform"),
        "known_namespace": state.get("namespace"),
        "known_service": state.get("service_name"),
        "raw_events": truncate_observation(
            state.get("raw_events", []), max_lines=40, max_bytes=3072
        ),
        "previous_execution_results": state.get("execution_results", []),
        "previous_verification_feedback": state.get("verification_feedback"),
    }

    messages: list[Any] = [
        SystemMessage(content=DIAGNOSE_SYSTEM_PROMPT),
        HumanMessage(content=json.dumps(context_payload, default=str)),
    ]
    collected_observations: list[dict[str, Any]] = []
    investigator_notes = ""

    max_steps = state.get("max_diagnose_steps", 6)
    for step_idx in range(1, max_steps + 1):
        ai_msg = await llm_with_tools.ainvoke(messages)
        messages.append(ai_msg)

        if getattr(ai_msg, "content", None):
            investigator_notes = str(ai_msg.content)

        if not getattr(ai_msg, "tool_calls", None):
            break

        for tc in ai_msg.tool_calls:
            tool_name = tc["name"]
            tool_args = tc.get("args") or {}
            tool_fn = tool_map.get(tool_name)

            if tool_fn is None:
                observation: Any = (
                    f"Error: Observation tool '{tool_name}' is not registered."
                )
            else:
                try:
                    logger.info(
                        "Incident %s [diagnose step %d] calling %s(%s)",
                        incident_id,
                        step_idx,
                        tool_name,
                        tool_args,
                    )
                    observation = await tool_fn.ainvoke(tool_args)
                except Exception as exc:
                    logger.warning(
                        "Incident %s observation tool %s error: %s",
                        incident_id,
                        tool_name,
                        exc,
                    )
                    observation = f"Tool error: {exc}"

            compact_obs = truncate_observation(
                observation, max_lines=80, max_bytes=4096
            )
            collected_observations.append(
                {
                    "step": step_idx,
                    "tool": tool_name,
                    "args": tool_args,
                    "output": compact_obs,
                }
            )
            messages.append(
                ToolMessage(
                    content=str(compact_obs),
                    tool_call_id=tc["id"],
                    name=tool_name,
                )
            )

    # Synthesize RCAResult using a clean, bounded [SystemMessage, HumanMessage] prompt
    # so providers like Gemini never receive dangling model turns or mismatched tool history.
    synthesis_payload = {
        "incident_context": context_payload,
        "available_mutation_tools": available_mutation_tools,
        "tool_observations": collected_observations,
        "investigator_notes": truncate_observation(
            investigator_notes, max_lines=40, max_bytes=2048
        ),
    }
    synthesis_messages = [
        SystemMessage(content=DIAGNOSE_SYSTEM_PROMPT),
        HumanMessage(
            content=(
                "Synthesize the final structured RCAResult using only the verified "
                "incident context and tool observations below:\n"
                + json.dumps(synthesis_payload, default=str)
            )
        ),
    ]

    structured_llm = llm.with_structured_output(RCAResult)
    rca: RCAResult = await structured_llm.ainvoke(synthesis_messages)

    target_uri = f"{rca.target.namespace_or_region}/{rca.target.resource_kind}/{rca.target.resource_name}"
    logger.info(
        "Incident %s diagnosed target=%s failure_type=%s confidence=%.2f",
        incident_id,
        target_uri,
        rca.failure_type,
        rca.confidence,
    )

    update: dict[str, Any] = {
        "platform": rca.target.platform,
        "namespace": rca.target.namespace_or_region,
        "resource_kind": rca.target.resource_kind,
        "service_name": rca.target.resource_name,
        "target_resource": target_uri,
        "failure_class": rca.failure_type,
        "suspected_cause": rca.root_cause_summary,
        "confidence": rca.confidence,
        "rca_result": rca.model_dump(),
        "status": "DIAGNOSED",
    }

    await sync_discovered_target_identity(incident_id, update)
    return update
