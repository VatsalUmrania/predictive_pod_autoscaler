"""
Lead Synthesizer coordinating multi-agent RCA investigation.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from nexus.graph.llm import get_llm
from nexus.graph.rca.log_analyst import LogAnalystAgent
from nexus.graph.rca.metrics_analyst import MetricsAnalystAgent
from nexus.graph.rca.prompts import SRE_SYSTEM_PROMPT
from nexus.graph.rca.result import RCAResult
from nexus.graph.rca.synthesis_rules import deterministic_synthesis
from nexus.graph.rca.topology_analyst import TopologyAnalystAgent

logger = logging.getLogger(__name__)


class RCALeadSynthesizer:
    """Blends multi-agent evidence into a verified RCAResult."""

    def __init__(self) -> None:
        self.log_analyst = LogAnalystAgent()
        self.metrics_analyst = MetricsAnalystAgent()
        self.topology_analyst = TopologyAnalystAgent()

    async def investigate(
        self,
        telemetry: dict[str, Any],
        events: list[dict[str, Any]],
        target: dict[str, Any],
        platform: str = "kubernetes",
    ) -> RCAResult:
        """Run multi-agent investigation over synthesized telemetry."""
        logs = telemetry.get("recent_logs", [])
        metrics = telemetry.get("metrics", {})
        live_config = telemetry.get("live_config", {})

        log_report = self.log_analyst.analyze(logs, events)
        metrics_report = self.metrics_analyst.analyze(metrics, events)
        topology_report = self.topology_analyst.analyze(events, live_config)

        all_signatures = set(
            log_report["signatures"]
            + metrics_report["signatures"]
            + topology_report["signatures"]
        )
        for evt in events:
            if isinstance(evt, dict) and evt.get("signal_type"):
                all_signatures.add(evt["signal_type"].lower())

        target_name = target.get("name", "workload")
        target_ns = target.get("namespace", "default")

        llm = get_llm()
        if hasattr(llm, "ainvoke") and type(llm).__name__ != "FakeListChatModel":
            try:
                describe_sample = str(live_config.get("describe", ""))[:2500]
                logs_sample = "\n".join(logs[-40:]) if logs else "No container logs captured"
                system_prompt = SRE_SYSTEM_PROMPT

                user_prompt = (
                    f"Target Resource: {platform} {target_ns}/{target_name}\n\n"
                    f"Log Analyst Findings:\n{json.dumps(log_report, indent=2, default=str)}\n\n"
                    f"Topology Analyst Findings:\n{json.dumps(topology_report, indent=2, default=str)}\n\n"
                    f"Metrics Analyst Findings:\n{json.dumps(metrics_report, indent=2, default=str)}\n\n"
                    f"Raw Trigger Events:\n{json.dumps(events[:5], indent=2, default=str)}\n\n"
                    f"Recent Container Logs (Tail):\n{logs_sample}\n\n"
                    f"Live Resource State & Events (Describe):\n{describe_sample}"
                )

                model_desc = getattr(llm, "model_name", getattr(llm, "model", getattr(llm, "_model", type(llm).__name__)))
                logger.info(
                    "[MultiAgentRCA] Invoking LLM (%s) for root cause analysis on %s/%s",
                    model_desc,
                    target_ns,
                    target_name,
                )

                from langchain_core.messages import HumanMessage, SystemMessage

                resp = await llm.ainvoke([SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)])
                raw_text = resp.content if hasattr(resp, "content") else str(resp)
                parsed = self._parse_llm_json(raw_text)
                if parsed:
                    return RCAResult(
                        root_cause=parsed.get("root_cause", f"Incident diagnosed via LLM on {target_name}"),
                        failure_class=parsed.get("failure_class", "unknown"),
                        healing_level=int(parsed.get("healing_level", 1)),
                        confidence=float(parsed.get("confidence", 0.85)),
                        reasoning=parsed.get("reasoning", "LLM multi-domain evidence synthesis"),
                        source="langgraph_multi_agent_llm",
                        domain=platform,
                        suggested_action=parsed.get("suggested_action"),
                        suggested_fix=parsed.get("suggested_fix"),
                        action_params=parsed.get("action_params", {"namespace": target_ns, "deployment_name": target_name}),
                        actions_to_avoid=parsed.get("actions_to_avoid", []),
                        evidence_citations=parsed.get("evidence_citations", log_report["findings"] + metrics_report["findings"]),
                    )
            except Exception as llm_exc:
                logger.error(
                    "[MultiAgentRCA] LLM invocation failed (%s: %s). Falling back to neuro-symbolic synthesis.",
                    type(llm_exc).__name__,
                    llm_exc,
                    exc_info=True,
                )

        return self._deterministic_synthesis(
            log_report=log_report,
            metrics_report=metrics_report,
            topology_report=topology_report,
            all_signatures=all_signatures,
            platform=platform,
            target_name=target_name,
            target_ns=target_ns,
        )

    def _deterministic_synthesis(
        self,
        log_report: dict[str, Any],
        metrics_report: dict[str, Any],
        topology_report: dict[str, Any],
        all_signatures: set[str],
        platform: str,
        target_name: str,
        target_ns: str,
    ) -> RCAResult:
        return deterministic_synthesis(
            log_report=log_report,
            metrics_report=metrics_report,
            topology_report=topology_report,
            all_signatures=all_signatures,
            platform=platform,
            target_name=target_name,
            target_ns=target_ns,
        )

    def _parse_llm_json(self, text: Any) -> dict[str, Any] | None:
        """Safely parse JSON from LLM output, extracting from markdown if needed."""
        if isinstance(text, dict):
            return text
        if isinstance(text, list):
            parts = [
                str(item.get("text", item)) if isinstance(item, dict) else str(item)
                for item in text
            ]
            text = "\n".join(parts)
        if not isinstance(text, str):
            text = str(text)

        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except Exception:
            pass

        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass

        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                data = json.loads(text[start : end + 1])
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return None
