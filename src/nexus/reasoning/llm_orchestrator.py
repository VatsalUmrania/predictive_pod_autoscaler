"""
LLM Orchestrator
================
Custom agent implementation using Gemini to diagnose and propose remediations
for Kubernetes cluster issues.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from google import genai
from google.genai import types

from nexus.agents import k8s_tools
from nexus.governance.policy_engine import LLM_TOOL_LEVEL
from nexus.integration.chatops import send_approval_request

logger = logging.getLogger(__name__)

api_key = os.getenv("NEXUS_LLM_API_KEY")

SYSTEM_PROMPT = """
You are the NEXUS AI Operator, an expert Kubernetes site reliability engineer (SRE).
Your task is to diagnose cluster issues based on Alertmanager webhooks and Kubernetes events.
You have access to read-only tools to fetch logs, events, metrics, and resource descriptions.
Use these tools to formulate a diagnosis and propose a remediation action.
Your proposed remediation should involve one of the available write-enabled tools (e.g., restart_deployment).
Do not execute write tools directly. Instead, output your diagnosis and the exact write tool you wish to call along with its parameters.
"""

class LLMOrchestrator:
    def __init__(self, model_name: str = "gemini-3.1-flash-lite"):
        self.model_name = model_name

        # Define the tools available to the LLM
        self.read_tools = [
            k8s_tools.get_pod_logs,
            k8s_tools.describe_resource,
            k8s_tools.get_events,
            k8s_tools.get_metrics,
            k8s_tools.get_yaml
        ]
        # Write tools must be registered with the model too, else Gemini can only
        # propose remediations as prose and the function-call → enqueue → approve
        # → execute path never fires. execute_remediation() below is the matching dispatch.
        self.write_tools = [
            k8s_tools.restart_deployment,
            k8s_tools.rollback_deployment,
            k8s_tools.patch_configmap,
            k8s_tools.scale_resource,
            k8s_tools.cordon_node,
            k8s_tools.drain_node
        ]

        self._client = None
        if api_key:
            try:
                self._client = genai.Client(api_key=api_key)
            except Exception as e:
                logger.error(f"Failed to initialize Gemini client: {e}")
                self._client = None

    def _ensure_client(self):
        if self._client is not None:
            return True
        if not api_key:
            return False
        try:
            self._client = genai.Client(api_key=api_key)
            return True
        except Exception as e:
            logger.error(f"Failed to initialize Gemini client: {e}")
            return False

    @staticmethod
    def _app_from_alert(payload: dict[str, Any]) -> str | None:
        """Best-effort app (deployment) name from an Alertmanager payload.

        Used so ``chatops.send_approval_request`` resolves the per-app webhook
        override via ``resolve_slack_webhook``; falls back to the global
        ``SLACK_WEBHOOK`` env when no per-app policy matches. Derives the app
        from the first alert's ``labels.pod`` (strip the replica ``-<rs>-<pod>``
        suffix) → else ``labels.namespace`` → else None.
        """
        alerts = payload.get("alerts") or []
        if alerts:
            labels = alerts[0].get("labels") or {}
            pod = labels.get("pod")
            if pod:
                # shop-demo-54f67c8b9d-abcde → shop-demo (drop last two -segments)
                parts = pod.rsplit("-", 2)
                if len(parts) == 3:
                    return parts[0]
                return pod
            ns = labels.get("namespace")
            if ns:
                return ns
        return None

    async def handle_alert(self, alert_payload: dict[str, Any]):
        """
        Entry point for handling an alert.
        """
        if not self._ensure_client():
            logger.error("Model not initialized. Cannot handle alert.")
            return

        logger.info(f"Handling alert: {alert_payload}")

        # Construct the initial prompt based on the alert
        prompt = f"An alert has been fired:\n{json.dumps(alert_payload, indent=2)}\nPlease diagnose the issue."

        try:
            chat = self._client.aio.chats.create(
                model=self.model_name,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    tools=self.read_tools,
                    temperature=0.2,
                    max_output_tokens=512,
                ),
            )
            response = await chat.send_message(prompt)
            diagnosis = response.text.strip() if response.text else str(response.function_calls)

            # Here we would parse the proposed remediation from the LLM's response
            # and send it to ChatOps for approval.
            # For simplicity, we just send the text.

            logger.info("Diagnosis complete. Sending to ChatOps for approval.")
            await send_approval_request(
                diagnosis=diagnosis,
                context=alert_payload,
                app_name=self._app_from_alert(alert_payload),
            )

        except Exception as e:
            logger.error(f"Error during LLM orchestration: {e}")

    def execute_remediation(self, tool_name: str, kwargs: dict[str, Any]) -> str:
        """Executes a write tool directly — UNGOVERNED.

        Kept for ad-hoc/diagnostic use only. The approval flow does NOT call
        this: an approved LLM action is re-dispatched through
        RunbookExecutor.execute_approved(), which wraps the same k8s_tools
        function with the ladder, cooldown, circuit breaker, audit trail, and
        rollback registry. Call this only if you intentionally want to bypass
        governance.
        """
        tool_func = next((t for t in self.write_tools if t.__name__ == tool_name), None)
        if tool_func is None:
            return f"Error: Tool {tool_name} not found."

        try:
            result = tool_func(**kwargs)
            return result
        except Exception as e:
            return f"Error executing tool {tool_name}: {e}"

    async def handle_cluster(
        self,
        cluster_summary: dict[str, Any],
        approval_queue: Any,
        confidence: float = 0.0,
        effective_level: int = 0,
    ) -> None:
        """
        Entry point for handling internal incident clusters.
        Processes cluster summary and proposes remediation via function calling.

        Args:
            confidence:      ConfidenceScorer-calibrated score for this cluster
                             (LLM_raw * w + signal_agreement + class_adj + …).
                             Stages the proposal at THIS confidence so the gate
                             (auto-run vs human-approval) is honest — never the
                             LLM's self-assessed confidence taken at face value.
            effective_level: Max level the scorer will permit for this cluster.
                             Caps an over-eager proposal: a cluster scored at
                             L1 can't stage a cordon (L3).
        """
        if not self._ensure_client():
            logger.error("Model not initialized. Cannot handle cluster.")
            return

        logger.info(
            f"Handling cluster: {cluster_summary} "
            f"(scored confidence={confidence:.2f}, effective_level=L{effective_level})"
        )

        # Construct the prompt for cluster analysis with explicit function calling instruction
        prompt = f"""
        Analyze the following Kubernetes cluster incident:

        {json.dumps(cluster_summary, indent=2)}

        Diagnose the issue and propose exactly ONE remediation action using the available tools.
        Your response MUST include a function call to one of the available write-enabled tools.
        Do not provide any additional text outside the function call.
        """

        try:
            chat = self._client.aio.chats.create(
                model=self.model_name,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    tools=self.read_tools + self.write_tools,
                    temperature=0.2,
                    max_output_tokens=512,
                ),
            )

            response = await chat.send_message(prompt)

            # Extract function calls from the response
            if hasattr(response, 'function_calls') and response.function_calls:
                function_call = response.function_calls[0]  # Take the first function call
                tool_name = function_call.name
                kwargs = dict(function_call.args)

                # Only write tools are actionable remediations; a read tool call
                # here is the model investigating, not proposing — skip enqueueing it.
                write_tool_names = {t.__name__ for t in self.write_tools}
                if tool_name not in write_tool_names:
                    logger.info(f"LLM returned read tool {tool_name} (not a remediation) — not staging")
                else:
                    logger.info(f"LLM proposed remediation: {tool_name} with args {kwargs}")

                    # Staged at the tool's nominal level + the scorer's
                    # calibrated confidence (never the hard-coded 0.8). The LLM
                    # path always enqueues for human review, so confidence here
                    # is the honest gate label the human sees + what
                    # execute_approved passes to the ladder. A cluster-wide L3
                    # proposal (cordon/drain) surfaces with its real blast.
                    spec = LLM_TOOL_LEVEL.get(tool_name)
                    tool_level = spec[0] if spec else 3  # unknown tool → L3 (safe)
                    blast_radius = spec[1] if spec else "cluster_wide"

                    # Stage the proposed action in the HumanApprovalQueue for human approval
                    approval_id = approval_queue.enqueue(
                        runbook_id="llm_dynamic",
                        action_type=tool_name,
                        target=kwargs.get("name", kwargs.get("deployment_name", "unknown")),
                        incident_id=cluster_summary.get("cluster_id", "unknown"),
                        healing_level=tool_level,
                        confidence=confidence,  # scored, not hard-coded 0.8
                        context={
                            "cluster_summary": cluster_summary,
                            "proposed_tool": tool_name,
                            "tool_args": kwargs,
                            "llm_diagnosis": str(response.text) if hasattr(response, 'text') and response.text else "See function call",
                            "tool_level": tool_level,
                            "blast_radius": blast_radius,
                            "effective_level": effective_level,
                        }
                    )

                    logger.info(f"Staged LLM remediation for approval: {approval_id}")

                    # Send to ChatOps with the approval_id so the message has
                    # functional Approve/Reject buttons (if SLACK_INTERACTIVE_URL
                    # is set) or CLI fallback.
                    await send_approval_request(
                        diagnosis=str(response.text) if hasattr(response, 'text') and response.text else "See function call",
                        context=cluster_summary,
                        approval_id=approval_id,
                    )
            else:
                # Fallback: no function call — send to ChatOps AND stage the
                # raw diagnosis for human review so /approvals/pending surfaces it.
                diagnosis = response.text.strip() if response.text else "No clear remediation proposed"
                logger.info(f"No function call in LLM response. Sending to ChatOps for manual review: {diagnosis}")

                approval_id = approval_queue.enqueue(
                    runbook_id="llm_dynamic",
                    action_type="manual_review",
                    target=cluster_summary.get("primary_resource", "unknown"),
                    incident_id=cluster_summary.get("cluster_id", "unknown"),
                    healing_level=0,
                    confidence=confidence,
                    context={
                        "cluster_summary": cluster_summary,
                        "llm_diagnosis": diagnosis,
                    }
                )

                await send_approval_request(
                    diagnosis=diagnosis,
                    context=cluster_summary,
                    approval_id=approval_id,
                )

        except Exception as e:
            logger.error(f"Error during LLM cluster orchestration: {e}")
            # Fallback to basic alerting on error
            await send_approval_request(
                diagnosis=f"LLM orchestration failed: {str(e)}",
                context=cluster_summary
            )

orchestrator = LLMOrchestrator()
