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
from typing import Any, Dict, List, Optional

from google import genai
from google.genai import types

from nexus.agents import k8s_tools
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

    async def handle_alert(self, alert_payload: Dict[str, Any]):
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
                context=alert_payload
            )

        except Exception as e:
            logger.error(f"Error during LLM orchestration: {e}")

    def execute_remediation(self, tool_name: str, kwargs: Dict[str, Any]) -> str:
        """
        Executes a write tool after human approval.
        Dispatch derives from self.write_tools (the same list registered with
        the model) so the two can never drift.
        """
        tool_func = next((t for t in self.write_tools if t.__name__ == tool_name), None)
        if tool_func is None:
            return f"Error: Tool {tool_name} not found."

        try:
            result = tool_func(**kwargs)
            return result
        except Exception as e:
            return f"Error executing tool {tool_name}: {e}"

    async def handle_cluster(self, cluster_summary: Dict[str, Any], approval_queue: Any) -> None:
        """
        Entry point for handling internal incident clusters.
        Processes cluster summary and proposes remediation via function calling.
        """
        if not self._ensure_client():
            logger.error("Model not initialized. Cannot handle cluster.")
            return

        logger.info(f"Handling cluster: {cluster_summary}")

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

                    # Stage the proposed action in the HumanApprovalQueue for human approval
                    approval_id = approval_queue.enqueue(
                        runbook_id="llm_dynamic",
                        action_type=tool_name,
                        target=kwargs.get("name", kwargs.get("deployment_name", "unknown")),
                        incident_id=cluster_summary.get("cluster_id", "unknown"),
                        healing_level=2,  # Default to L2 for LLM-proposed actions
                        confidence=0.8,   # Default confidence for LLM proposals
                        context={
                            "cluster_summary": cluster_summary,
                            "proposed_tool": tool_name,
                            "tool_args": kwargs,
                            "llm_diagnosis": str(response.text) if hasattr(response, 'text') and response.text else "See function call"
                        }
                    )

                    logger.info(f"Staged LLM remediation for approval: {approval_id}")
            else:
                # Fallback: no function call — send to ChatOps AND stage the
                # raw diagnosis for human review so /approvals/pending surfaces it.
                diagnosis = response.text.strip() if response.text else "No clear remediation proposed"
                logger.info(f"No function call in LLM response. Sending to ChatOps for manual review: {diagnosis}")

                approval_queue.enqueue(
                    runbook_id="llm_dynamic",
                    action_type="manual_review",
                    target=cluster_summary.get("primary_resource", "unknown"),
                    incident_id=cluster_summary.get("cluster_id", "unknown"),
                    healing_level=0,
                    confidence=0.0,
                    context={
                        "cluster_summary": cluster_summary,
                        "llm_diagnosis": diagnosis,
                    }
                )

                await send_approval_request(
                    diagnosis=diagnosis,
                    context=cluster_summary
                )

        except Exception as e:
            logger.error(f"Error during LLM cluster orchestration: {e}")
            # Fallback to basic alerting on error
            await send_approval_request(
                diagnosis=f"LLM orchestration failed: {str(e)}",
                context=cluster_summary
            )

orchestrator = LLMOrchestrator()
