"""
NEXUS Multi-Agent Root Cause Analysis (LangGraph RCA Subsystem)
================================================================
Investigates incident telemetry using specialized domain agents:
  1. LogAnalystAgent: Deep inspection of stdout/stderr logs, panics, and exit codes.
  2. MetricsAnalystAgent: Anomaly evaluation of CPU, memory, error rates, and latencies.
  3. TopologyAnalystAgent: Correlation with recent deployments, config changes, and dependencies.
  4. RCALeadSynthesizer: Multi-agent synthesis into a validated RCAResult.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from nexus.graph.llm import get_llm
from nexus.graph.rca.result import RCAResult, VALID_FAILURE_CLASSES

logger = logging.getLogger(__name__)


class LogAnalystAgent:
    """Specialist agent analyzing container logs, stack traces, and exit codes."""

    def analyze(self, logs: list[str], raw_signals: list[dict[str, Any]]) -> dict[str, Any]:
        findings: list[str] = []
        signatures: list[str] = []
        confidence_delta = 0.0

        combined = "\n".join(logs).lower()

        # Startup crashes / immediate exit codes / broken commands
        startup_crash_terms = ("crashing", "command failed", "fatal startup", "cannot execute", "command not found")
        has_startup_crash = any(term in combined for term in startup_crash_terms) or bool(
            re.search(r"\b(?:exit(?:ed with)? code 1|exit 1)\b", combined)
        )
        if has_startup_crash:
            findings.append("Container logs indicate immediate startup termination / exit code 1")
            signatures.append("startup_crash")
            signatures.append("crashloop")
            confidence_delta += 0.30

        # OOM detection
        if any(term in combined for term in ("oomkilled", "fatal memory error", "out of memory", "heap out of memory", "exit code 137")):
            findings.append("Log indicates memory exhaustion / OOM kill")
            signatures.append("oom_event")
            confidence_delta += 0.35

        # Panics and unhandled exceptions
        if any(term in combined for term in ("panic:", "traceback (most recent call last)", "fatal error:", "nullpointerexception", "segmentation fault")):
            findings.append("Log contains fatal application panic or unhandled exception trace")
            signatures.append("runtime_panic")
            confidence_delta += 0.25

        # Connection / Database pool errors
        if any(term in combined for term in ("connection pool exhausted", "too many connections", "connection refused", "dial tcp: i/o timeout", "remaining connection slots")):
            findings.append("Log indicates downstream connection pool or network socket exhaustion")
            signatures.append("connection_exhaustion")
            confidence_delta += 0.25

        # Missing configuration / Env keys
        if any(term in combined for term in ("keyerror", "missing environment variable", "env var not set", "cannot find module")):
            findings.append("Log indicates missing configuration or environment variable")
            signatures.append("missing_config")
            confidence_delta += 0.30

        return {
            "agent": "LogAnalyst",
            "findings": findings,
            "signatures": signatures,
            "confidence_delta": confidence_delta,
            "has_critical_error": bool(signatures),
        }


class MetricsAnalystAgent:
    """Specialist agent analyzing saturation, error rates, latency, and resource metrics."""

    def analyze(self, metrics: dict[str, Any], raw_signals: list[dict[str, Any]]) -> dict[str, Any]:
        findings: list[str] = []
        signatures: list[str] = []
        confidence_delta = 0.0

        error_rate = float(metrics.get("error_rate", 0.0))
        p99_latency_ms = float(metrics.get("p99_latency_ms", metrics.get("latency_p99", 0.0)))
        memory_usage_pct = float(metrics.get("memory_usage_pct", metrics.get("memory_usage", 0.0)))
        cpu_usage_pct = float(metrics.get("cpu_usage_pct", metrics.get("cpu_usage", 0.0)))
        restarts = int(metrics.get("restart_count", 0))

        if error_rate >= 0.05 or any(s.get("signal_type") == "high_error_rate" for s in raw_signals):
            findings.append(f"Elevated HTTP/gRPC error rate: {error_rate:.1%}")
            signatures.append("elevated_error_rate")
            confidence_delta += 0.20

        if p99_latency_ms > 1000.0 or any(s.get("signal_type") == "high_latency" for s in raw_signals):
            findings.append(f"Severe latency degradation (p99={p99_latency_ms:.0f}ms)")
            signatures.append("latency_spike")
            confidence_delta += 0.15

        if memory_usage_pct > 85.0:
            findings.append(f"Memory saturation critical: {memory_usage_pct:.1f}%")
            signatures.append("memory_saturation")
            confidence_delta += 0.20

        if restarts > 0:
            findings.append(f"Pod restart count actively incrementing: {restarts}")
            signatures.append("pod_restarts")
            confidence_delta += 0.20

        return {
            "agent": "MetricsAnalyst",
            "findings": findings,
            "signatures": signatures,
            "confidence_delta": confidence_delta,
            "metrics_summary": {
                "error_rate": error_rate,
                "p99_latency_ms": p99_latency_ms,
                "memory_pct": memory_usage_pct,
                "cpu_pct": cpu_usage_pct,
                "restarts": restarts,
            },
        }


class TopologyAnalystAgent:
    """Specialist agent correlating deployments, git revisions, and platform dependencies."""

    def analyze(self, raw_signals: list[dict[str, Any]], live_config: dict[str, Any]) -> dict[str, Any]:
        findings: list[str] = []
        signatures: list[str] = []
        has_recent_deploy = False

        # 1. Correlate raw signals
        for sig in raw_signals:
            stype = str(sig.get("signal_type", "")).lower()
            if stype in ("deploy_event", "rollout_started", "image_updated"):
                has_recent_deploy = True
                findings.append(f"Recent deployment detected: {sig.get('resource_name', 'unknown')}")
                signatures.append("recent_deploy")
            elif stype in ("pod_crashloop", "crashloopbackoff"):
                signatures.append("crashloop")
            elif stype in ("pod_oomkilled", "oomkilled"):
                signatures.append("oom_signal")
            elif "dns" in stype:
                signatures.append("dns_anomaly")
                findings.append("DNS resolution anomalies detected in network topology")
            elif "db" in stype or "database" in stype:
                signatures.append("database_anomaly")
                findings.append("Database tier reporting connection saturation")

        # 2. Correlate live resource describe state
        desc = str(live_config.get("describe", "")).lower()
        if desc:
            if "active rollout=true" in desc or "replicasets: 2" in desc or "scalingreplicaset" in desc or "newreplicaset" in desc:
                has_recent_deploy = True
                findings.append("Active Kubernetes deployment rollout detected across multiple ReplicaSets")
                signatures.append("recent_deploy")
                signatures.append("rollout_stuck")

            if "command:" in desc or "exit 1" in desc or "crashing" in desc or "command override" in desc:
                has_recent_deploy = True
                findings.append("Container spec command override or broken startup script detected in deployment template")
                signatures.append("recent_deploy")
                signatures.append("spec_mutation")

            if "crashloopbackoff" in desc or "reason: error" in desc or "exit code 1" in desc:
                signatures.append("crashloop")
                findings.append("Container termination with non-zero exit code observed in workload describe")

        return {
            "agent": "TopologyAnalyst",
            "findings": findings,
            "signatures": signatures,
            "has_recent_deploy": has_recent_deploy,
        }


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

        all_signatures = set(log_report["signatures"] + metrics_report["signatures"] + topology_report["signatures"])
        for evt in events:
            if isinstance(evt, dict) and evt.get("signal_type"):
                all_signatures.add(evt["signal_type"].lower())

        target_name = target.get("name", "workload")
        target_ns = target.get("namespace", "default")

        # 1. Primary Reasoning Path: Autonomous LLM SRE Synthesis
        llm = get_llm()
        if hasattr(llm, "ainvoke") and getattr(llm, "__class__", None).__name__ != "FakeListChatModel":
            try:
                describe_sample = str(live_config.get("describe", ""))[:2500]
                logs_sample = "\n".join(logs[-40:]) if logs else "No container logs captured"

                system_prompt = (
                    "You are the NEXUS Autonomous Lead SRE Diagnostician. Analyze the multi-domain telemetry, "
                    "actual container stdout/stderr logs, live Kubernetes describe output, and agent findings "
                    "for this production incident.\n\n"
                    "CORE SRE REASONING PRINCIPLES:\n"
                    "1. Directly inspect the container logs and Kubernetes describe output.\n"
                    "2. If a container command/entrypoint failed on startup (e.g. non-zero exit code, exit 1, "
                    "syntax error, broken script, missing command), or if an active rollout / recent deployment "
                    "introduced a breaking change, diagnose failure_class='bad_deploy' and suggested_action='k8s_rollback_deployment' "
                    "(or 'k8s_remove_command_override' if a faulty container command override was introduced).\n"
                    "3. Provide a clear, actionable 'suggested_fix' explaining exactly what remediation will do "
                    "(e.g. 'Roll back deployment shop-demo to previous stable ReplicaSet revision to remove faulty container command override and restore availability').\n"
                    "4. CRITICAL: Do NOT suggest 'k8s_restart_deployment' if the container spec or startup command itself is faulty. "
                    "A restart will only reproduce the exact same crashloop. Add 'k8s_restart_deployment' to actions_to_avoid.\n"
                    "5. If memory limits were breached or OOMKilled was recorded, diagnose failure_class='resource_exhaustion' "
                    "and suggest 'k8s_scale_deployment' or 'k8s_restart_deployment'.\n"
                    "6. If configuration or environment variables are missing or corrupted, diagnose failure_class='config_error' "
                    "and suggest 'k8s_patch_configmap'.\n"
                    "7. If high traffic load caused latency/error breaches without crashes, suggest 'k8s_scale_deployment'.\n\n"
                    "Rules: Output ONLY a valid JSON object matching this schema:\n"
                    "{\n"
                    '  "root_cause": "1-2 sentences technical root cause based on the concrete evidence",\n'
                    '  "failure_class": "bad_deploy | resource_exhaustion | dependency_failure | config_error | cascading_failure | unknown",\n'
                    '  "healing_level": 1 or 2,\n'
                    '  "confidence": 0.85,\n'
                    '  "reasoning": "Detailed SRE multi-step chain-of-thought analysis",\n'
                    '  "suggested_action": "k8s_rollback_deployment | k8s_remove_command_override | k8s_restart_deployment | k8s_scale_deployment | k8s_patch_configmap | aws_update_lambda_memory | aws_update_lambda_timeout | aws_rollback_lambda_alias",\n'
                    '  "suggested_fix": "1-2 sentences plain-English executive summary describing the concrete remediation action and expected outcome",\n'
                    '  "action_params": {"namespace": "...", "deployment_name": "..."},\n'
                    '  "actions_to_avoid": ["list of dangerous or ineffective actions, e.g. k8s_restart_deployment"],\n'
                    '  "evidence_citations": ["specific quotes from container logs or describe output"]\n'
                    "}"
                )

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
                    "🤖 [MultiAgentRCA] Invoking LLM (%s) for root cause analysis on %s/%s",
                    model_desc,
                    target_ns,
                    target_name,
                )

                from langchain_core.messages import HumanMessage, SystemMessage
                resp = await llm.ainvoke([SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)])
                raw_text = resp.content if hasattr(resp, "content") else str(resp)
                parsed = self._parse_llm_json(raw_text)
                if parsed:
                    logger.info(
                        "🧠 [MultiAgentRCA] LLM Diagnosis succeeded: failure_class=%s, action=%s, conf=%.2f\n  Root Cause: %s\n  Suggested Fix: %s",
                        parsed.get("failure_class"),
                        parsed.get("suggested_action"),
                        parsed.get("confidence", 0.85),
                        parsed.get("root_cause"),
                        parsed.get("suggested_fix"),
                    )
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
                else:
                    logger.warning("[MultiAgentRCA] LLM returned non-JSON response:\n%s", raw_text[:300])
            except Exception as llm_exc:
                logger.error(
                    "❌ [MultiAgentRCA] LLM invocation failed (%s: %s). Falling back to neuro-symbolic synthesis.",
                    type(llm_exc).__name__,
                    llm_exc,
                    exc_info=True,
                )

        # 2. Neuro-Symbolic Fallback (For offline unit tests or when LLM API is unreachable)
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
        """Dynamic fallback synthesis for test environments where LLM API is unavailable."""
        evidence: list[str] = log_report["findings"] + metrics_report["findings"] + topology_report["findings"]

        # Case 1: Bad Deployment / Spec Mutation (Rollout or spec change correlated with crashes)
        is_deploy_related = (
            topology_report.get("has_recent_deploy")
            or "recent_deploy" in all_signatures
            or "spec_mutation" in all_signatures
            or "rollout_stuck" in all_signatures
            or "startup_crash" in all_signatures
        )
        has_crash_or_errors = (
            "crashloop" in all_signatures
            or "pod_crashloop" in all_signatures
            or "elevated_error_rate" in all_signatures
            or "startup_crash" in all_signatures
        )

        if is_deploy_related and has_crash_or_errors:
            return RCAResult(
                root_cause=f"Deployment rollout or container spec modification to {target_name} introduced a fatal regression causing startup crashes.",
                failure_class="bad_deploy",
                healing_level=2,
                confidence=0.88,
                reasoning="Topology and log analysts confirmed deployment rollout or spec mutation coincided with startup failure.",
                source="langgraph_multi_agent",
                domain=platform,
                suggested_action="k8s_rollback_deployment" if platform == "kubernetes" else "aws_rollback_lambda_alias",
                suggested_fix=f"Roll back deployment {target_name} to previous stable revision to remove breaking deployment changes and restore pod readiness.",
                action_params={"namespace": target_ns, "deployment_name": target_name} if platform == "kubernetes" else {"function_name": target_name},
                actions_to_avoid=["k8s_restart_deployment", "k8s_scale_deployment", "ignore"],
                evidence_citations=evidence,
            )

        # Case 2: Memory Exhaustion / OOMKilled
        if any(s in all_signatures for s in ("oom_event", "oom_signal", "memory_saturation", "pod_oomkilled", "lambda_oom")):
            action = "k8s_restart_deployment" if platform == "kubernetes" else "aws_update_lambda_memory"
            params = {"namespace": target_ns, "deployment_name": target_name} if platform == "kubernetes" else {"function_name": target_name, "memory_mb": 512, "region": target_ns}
            return RCAResult(
                root_cause=f"Resource memory limit exceeded for {target_name}.",
                failure_class="resource_exhaustion",
                healing_level=1,
                confidence=0.92,
                reasoning="Log/metric analysts identified critical memory exhaustion / OOM signatures.",
                source="langgraph_multi_agent",
                domain=platform,
                suggested_action=action,
                suggested_fix=f"Restart or scale {target_name} to clear transient memory exhaustion and re-balance pod memory usage.",
                action_params=params,
                actions_to_avoid=["traffic_increase"],
                evidence_citations=evidence,
            )

        # Case 3: Lambda Execution Timeout
        if "lambda_timeout" in all_signatures or "timeout_event" in all_signatures:
            return RCAResult(
                root_cause=f"Lambda function {target_name} execution exceeded timeout limit.",
                failure_class="resource_exhaustion",
                healing_level=1,
                confidence=0.88,
                reasoning="CloudWatch metrics/logs show function runtime approaching maximum duration.",
                source="langgraph_multi_agent",
                domain=platform,
                suggested_action="aws_update_lambda_timeout",
                suggested_fix=f"Increase Lambda function {target_name} execution timeout to 60s to accommodate processing duration.",
                action_params={"function_name": target_name, "timeout_seconds": 60, "region": target_ns},
                evidence_citations=evidence,
            )

        # Case 4: Missing Environment Variables / Config
        if "missing_config" in all_signatures or "env_contract_violation" in all_signatures:
            return RCAResult(
                root_cause=f"Required environment variables or configuration values missing in {target_name}.",
                failure_class="config_error",
                healing_level=2,
                confidence=0.90,
                reasoning="Log analyst detected missing environment variables preventing application startup.",
                source="langgraph_multi_agent",
                domain=platform,
                suggested_action="k8s_patch_configmap" if platform == "kubernetes" else None,
                suggested_fix=f"Patch configuration or environment variables for {target_name} to resolve startup dependency errors.",
                action_params={"namespace": target_ns, "deployment_name": target_name},
                evidence_citations=evidence,
            )

        # Case 5: Database / Dependency Connection Exhaustion
        if any(s in all_signatures for s in ("connection_exhaustion", "database_anomaly", "db_connection_exhaustion")):
            return RCAResult(
                root_cause=f"Downstream database connection pool is exhausted for {target_name}.",
                failure_class="resource_exhaustion",
                healing_level=2,
                confidence=0.82,
                reasoning="Log analyst identified connection pool errors and timeout traces.",
                source="langgraph_multi_agent",
                domain=platform,
                suggested_action="k8s_restart_deployment" if platform == "kubernetes" else None,
                suggested_fix=f"Restart {target_name} to clear stale database pool connections and re-establish downstream pools.",
                action_params={"namespace": target_ns, "deployment_name": target_name},
                actions_to_avoid=["k8s_scale_deployment"],
                evidence_citations=evidence,
            )

        # Case 6: General CrashLoop / Runtime Panics
        if any(s in all_signatures for s in ("crashloop", "runtime_panic", "pod_crashloop")):
            return RCAResult(
                root_cause=f"Application pods for {target_name} encountered an unhandled runtime error or crashloop.",
                failure_class="bad_deploy",
                healing_level=1,
                confidence=0.90,
                reasoning="Log analyst identified crashloop backoff trace.",
                source="langgraph_multi_agent",
                domain=platform,
                suggested_action="k8s_restart_deployment" if platform == "kubernetes" else None,
                action_params={"namespace": target_ns, "deployment_name": target_name},
                evidence_citations=evidence,
            )

        # Case 7: High Error Rate Spikes
        if "elevated_error_rate" in all_signatures or "high_error_rate" in all_signatures:
            return RCAResult(
                root_cause=f"HTTP error rate spike in {target_name} exceeding SLO threshold.",
                failure_class="cascading_failure",
                healing_level=1,
                confidence=0.78,
                reasoning="Metrics analyst measured error rate breach without clear fatal log signatures.",
                source="langgraph_multi_agent",
                domain=platform,
                suggested_action="k8s_scale_deployment" if platform == "kubernetes" else None,
                action_params={"namespace": target_ns, "deployment_name": target_name, "replicas": 3},
                evidence_citations=evidence,
            )

        # Default fallback
        return RCAResult(
            root_cause=f"Anomalous operational signals detected on {target_name}.",
            failure_class="unknown",
            healing_level=0,
            confidence=0.50,
            reasoning="Multi-agent evidence is inconclusive; escalating for human investigation.",
            source="langgraph_multi_agent",
            domain=platform,
            evidence_citations=evidence,
        )

    def _parse_llm_json(self, text: Any) -> dict[str, Any] | None:
        """Safely parse JSON from LLM output, extracting from markdown if needed."""
        if isinstance(text, dict):
            return text

        if isinstance(text, list):
            parts: list[str] = []
            for item in text:
                if isinstance(item, dict) and "text" in item:
                    parts.append(str(item["text"]))
                elif isinstance(item, str):
                    parts.append(item)
                else:
                    parts.append(str(item))
            text = "\n".join(parts)

        if not isinstance(text, str):
            text = str(text)

        try:
            return json.loads(text)
        except Exception:
            pass

        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except Exception:
                pass

        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except Exception:
                pass

        return None
