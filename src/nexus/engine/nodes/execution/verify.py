import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from nexus.engine.nodes.analysis.diagnose_builders import truncate_observation
from nexus.engine.reasoning.rca.result import VerificationResult
from nexus.engine.tools.registry import get_tool_registry
from nexus.engine.workflow.llm.llm import get_chat_model
from nexus.engine.workflow.state.state import IncidentState

logger = logging.getLogger(__name__)

VERIFY_SYSTEM_PROMPT = """You are the NEXUS Post-Remediation Verification Agent.
Remediation tool calls have just been executed on the target workload.
Use your read-only observation tools to inspect the live state of the target workload (`namespace`, `resource_kind`, `service_name`):
1. Check if pods/instances are Running and passing their Readiness/Liveness probes.
2. Verify that `readyReplicas` matches desired replicas and no new CrashLoopBackOff or probe failure events are occurring.
3. Synthesize a structured VerificationResult indicating whether the remediation succeeded (`verification_passed = True`) or failed (`verification_passed = False` with detailed `failure_feedback`)."""


async def verify_node(state: IncidentState) -> dict[str, Any]:
    incident_id = state.get("incident_id", "unknown")
    registry = get_tool_registry()
    observe_tools = registry.get_tools(mode="observe", platform=state.get("platform"))
    tool_map = {t.name: t for t in observe_tools}

    llm = get_chat_model(temperature=0.0)
    llm_with_tools = llm.bind_tools(observe_tools) if observe_tools else llm

    verify_context = {
        "incident_id": incident_id,
        "target": {
            "platform": state.get("platform"),
            "namespace": state.get("namespace"),
            "resource_kind": state.get("resource_kind"),
            "service_name": state.get("service_name"),
        },
        "root_cause": state.get("suspected_cause"),
        "executed_steps": state.get("execution_results", []),
    }

    messages: list[Any] = [
        SystemMessage(content=VERIFY_SYSTEM_PROMPT),
        HumanMessage(content=json.dumps(verify_context, default=str)),
    ]
    collected_observations: list[dict[str, Any]] = []
    verifier_notes = ""

    for step_idx in range(1, 5):
        ai_msg = await llm_with_tools.ainvoke(messages)
        messages.append(ai_msg)

        if getattr(ai_msg, "content", None):
            verifier_notes = str(ai_msg.content)

        if not getattr(ai_msg, "tool_calls", None):
            break

        for tc in ai_msg.tool_calls:
            tool_fn = tool_map.get(tc["name"])
            if tool_fn is None:
                obs: Any = f"Error: Observation tool '{tc['name']}' not found."
            else:
                try:
                    obs = await tool_fn.ainvoke(tc.get("args") or {})
                except Exception as exc:
                    obs = f"Tool error: {exc}"
            compact_obs = truncate_observation(obs, max_lines=80, max_bytes=4096)
            collected_observations.append(
                {
                    "step": step_idx,
                    "tool": tc["name"],
                    "args": tc.get("args") or {},
                    "output": compact_obs,
                }
            )
            messages.append(
                ToolMessage(
                    content=str(compact_obs), tool_call_id=tc["id"], name=tc["name"]
                )
            )

    synthesis_payload = {
        "verify_context": verify_context,
        "tool_observations": collected_observations,
        "verifier_notes": truncate_observation(
            verifier_notes, max_lines=40, max_bytes=2048
        ),
    }
    synthesis_messages = [
        SystemMessage(content=VERIFY_SYSTEM_PROMPT),
        HumanMessage(
            content=(
                "Synthesize the final structured VerificationResult using only the "
                "context and live tool observations below:\n"
                + json.dumps(synthesis_payload, default=str)
            )
        ),
    ]

    structured_llm = llm.with_structured_output(VerificationResult)
    verification: VerificationResult = await structured_llm.ainvoke(synthesis_messages)

    logger.info(
        "Incident %s verification_passed=%s summary=%s",
        incident_id,
        verification.verification_passed,
        verification.observed_state_summary,
    )

    return {
        "verification_passed": verification.verification_passed,
        "verification_feedback": verification.failure_feedback
        or verification.observed_state_summary,
        "status": (
            "RESOLVED" if verification.verification_passed else "VERIFICATION_FAILED"
        ),
    }
