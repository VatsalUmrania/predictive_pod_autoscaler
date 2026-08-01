"""
NEXUS RCA Engine
=================
Gemini-powered Root Cause Analysis with a deterministic rule-based fallback.

Two-layer architecture:
    Layer 1 — Gemini 1.5 Flash (primary)
        • Sends a structured incident report as a prompt
        • Requests JSON output with specific schema
        • Low temperature (0.2) for reproducibility
        • Timeout: 10s

    Layer 2 — Rule-based fallback (when Gemini unavailable or API key missing)
        • Pattern matching on signal_type sets
        • Deterministic, fully auditable
        • Conservative confidence scores

RCAResult fields:
    root_cause:    Human-readable explanation of the failure
    failure_class: bad_deploy | resource_exhaustion | dependency_failure |
                   config_error | cascading_failure | unknown
    healing_level: 0-3 (maps directly to ActionLadder levels)
    runbook_id:    Suggested runbook ID from RunbookLibrary (may be None)
    confidence:    Raw estimate 0.0-1.0 (calibrated further by ConfidenceScorer)
    reasoning:     Chain-of-thought from LLM (rule label from fallback)
    source:        "gemini" | "rule_based"

Configuration:
    NEXUS_LLM_API_KEY     — required for Gemini; fallback used if absent
    NEXUS_GEMINI_MODEL     — model name (default: gemini-3.1-flash-lite)
    NEXUS_RCA_TIMEOUT_S    — Gemini request timeout in seconds (default: 10)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from nexus.reasoning.incident_cluster import IncidentCluster

logger = logging.getLogger(__name__)

# RCA Result
VALID_FAILURE_CLASSES = frozenset(
    {
        "bad_deploy",
        "resource_exhaustion",
        "dependency_failure",
        "config_error",
        "cascading_failure",
        "unknown",
    }
)

VALID_HEALING_LEVELS = (0, 1, 2, 3)

@dataclass
class RCAResult:
    """The output of the RCA Engine for one IncidentCluster."""

    root_cause: str
    failure_class: str
    healing_level: int
    runbook_id: str | None
    confidence: float
    reasoning: str
    source: str  # "gemini" | "rule_based"
    actions_to_avoid: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Clamp and validate
        self.confidence = max(0.0, min(1.0, self.confidence))
        self.healing_level = max(0, min(3, self.healing_level))
        if self.failure_class not in VALID_FAILURE_CLASSES:
            self.failure_class = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_cause": self.root_cause,
            "failure_class": self.failure_class,
            "healing_level": self.healing_level,
            "runbook_id": self.runbook_id,
            "confidence": round(self.confidence, 3),
            "reasoning": self.reasoning,
            "source": self.source,
            "actions_to_avoid": self.actions_to_avoid,
        }

    def __str__(self) -> str:
        return (
            f"RCAResult(class={self.failure_class}, L{self.healing_level}, "
            f"confidence={self.confidence:.2f}, runbook={self.runbook_id}, "
            f"src={self.source})"
        )

# Rule-based fallback
# Rules are (required_signals, partial_ok, result_template)
# required_signals ⊆ cluster.signal_types for the rule to match.
# partial_ok=True means ANY signal in the set triggers the rule (OR match).
# partial_ok=False means ALL signals required (AND match).
# Rules are evaluated top-to-bottom; first match wins.

_RULES: list[tuple[frozenset[str], bool, dict[str, Any]]] = [
    # Highest confidence first
    # 1. ENV contract violation — deterministic, always L0 block
    (
        frozenset({"env_contract_violation"}),
        False,
        {
            "root_cause": "Required environment variables are missing from the deployment. "
            "The container will fail to start without them.",
            "failure_class": "config_error",
            "healing_level": 0,
            "runbook_id": "runbook_missing_env_key_v1",
            "confidence": 0.97,
            "reasoning": "ENV_CONTRACT_VIOLATION is a deterministic signal — "
            "the AST scanner found required keys absent from secrets/env.",
        },
    ),
    # 2. Secret accidentally committed — security incident, L0 (alert immediately)
    (
        frozenset({"secret_committed"}),
        False,
        {
            "root_cause": "A potential secret or credential was detected in a recent git commit.",
            "failure_class": "config_error",
            "healing_level": 0,
            "runbook_id": None,
            "confidence": 0.90,
            "reasoning": "SECRET_COMMITTED detected in diff — "
            "require immediate manual revocation and secret rotation.",
        },
    ),
    # 3. OOMKilled — clearest resource signal
    (
        frozenset({"pod_oomkilled"}),
        False,
        {
            "root_cause": "Pod terminated by the OOM killer — memory limit exceeded. "
            "Container needs higher memory limits or there is a memory leak.",
            "failure_class": "resource_exhaustion",
            "healing_level": 1,
            "runbook_id": "runbook_pod_crashloop_v1",
            "confidence": 0.90,
            "reasoning": "OOMKilled is a deterministic K8s signal. "
            "VPA hint + pod restart is the appropriate first response.",
        },
    ),
    # 4. Pod crash + deploy event (most specific compound rule)
    (
        frozenset({"pod_crashloop", "deploy_event"}),
        True,
        {
            "root_cause": "Pod crash loop correlated with a recent deployment — "
            "the new image or config change is likely causing startup failures.",
            "failure_class": "bad_deploy",
            "healing_level": 2,
            "runbook_id": "runbook_high_error_rate_post_deploy_v1",
            "confidence": 0.83,
            "reasoning": "CrashLoopBackOff coinciding with a deploy event "
            "is the canonical 'bad deploy' pattern. "
            "Canary halt + rollout undo is the appropriate response.",
        },
    ),
    # 5. High error rate + deploy event
    (
        frozenset({"high_error_rate", "deploy_event"}),
        True,
        {
            "root_cause": "HTTP error rate spike starting after a recent deployment — "
            "regression in the new code version.",
            "failure_class": "bad_deploy",
            "healing_level": 2,
            "runbook_id": "runbook_high_error_rate_post_deploy_v1",
            "confidence": 0.82,
            "reasoning": "Error rate correlated with deploy time is a strong "
            "bad-deploy signal. Halting canary traffic protects users.",
        },
    ),
    # 6. Pod crash alone
    (
        frozenset({"pod_crashloop"}),
        False,
        {
            "root_cause": "Pod repeatedly crashing on startup — likely due to misconfiguration, "
            "missing dependency, or application bug.",
            "failure_class": "bad_deploy",
            "healing_level": 1,
            "runbook_id": "runbook_pod_crashloop_v1",
            "confidence": 0.82,
            "reasoning": "CrashLoopBackOff is a clear K8s failure signal. "
            "Pod restart resets the exponential backoff timer.",
        },
    ),
    # 7. DB connection exhaustion
    (
        frozenset({"db_connection_exhaustion"}),
        False,
        {
            "root_cause": "Database connection pool is exhausted — connections are not "
            "being released or traffic spike exceeded pool capacity.",
            "failure_class": "resource_exhaustion",
            "healing_level": 2,
            "runbook_id": "runbook_db_connection_exhaustion_v1",
            "confidence": 0.80,
            "reasoning": "DB connection utilization at threshold is a resource "
            "exhaustion signal. Alerting + annotation allows DBAs to investigate.",
        },
    ),
    # 8. DNS resolution failure
    (
        frozenset({"dns_resolution_failure"}),
        False,
        {
            "root_cause": "DNS resolution failures detected — CoreDNS may be "
            "overloaded or its cache is corrupted.",
            "failure_class": "dependency_failure",
            "healing_level": 1,
            "runbook_id": "runbook_dns_resolution_failure_v1",
            "confidence": 0.77,
            "reasoning": "DNS failures affect all services in the namespace. "
            "CoreDNS cache flush is safe and typically resolves stale records.",
        },
    ),
    # 9. HPA maxed out
    (
        frozenset({"hpa_maxed"}),
        False,
        {
            "root_cause": "HPA has reached maximum replica count — cluster cannot "
            "auto-scale further to handle the current load.",
            "failure_class": "resource_exhaustion",
            "healing_level": 0,
            "runbook_id": None,
            "confidence": 0.72,
            "reasoning": "HPA saturation is a capacity ceiling problem. "
            "Autonomous healing cannot increase cluster capacity — escalate.",
        },
    ),
    # 10. Deployment degraded
    (
        frozenset({"deployment_degraded"}),
        False,
        {
            "root_cause": "Deployment has fewer available replicas than desired — "
            "pods failing to start or being evicted.",
            "failure_class": "bad_deploy",
            "healing_level": 1,
            "runbook_id": "runbook_pod_crashloop_v1",
            "confidence": 0.68,
            "reasoning": "Degraded deployment often results from pod startup failures. "
            "Pod restart with logging is the first investigation step.",
        },
    ),
    # 11. High error rate alone (no deploy correlated)
    (
        frozenset({"high_error_rate"}),
        False,
        {
            "root_cause": "Elevated HTTP error rate with no correlated deployment — "
            "possible upstream dependency failure or transient spike.",
            "failure_class": "unknown",
            "healing_level": 0,
            "runbook_id": None,
            "confidence": 0.50,
            "reasoning": "Error rate elevated but no deploy detected. "
            "Alerting only — more signals needed before autonomous action.",
        },
    ),
    # 12. Rollout stuck
    (
        frozenset({"rollout_stuck"}),
        False,
        {
            "root_cause": "Kubernetes deployment rollout has exceeded its progress deadline — "
            "new pods are not becoming ready.",
            "failure_class": "bad_deploy",
            "healing_level": 3,
            "runbook_id": None,  # Rollout undo is available but requires high confidence
            "confidence": 0.75,
            "reasoning": "ProgressDeadlineExceeded is a K8s rollout failure signal. "
            "Manual rollback evaluation is recommended at L3.",
        },
    ),
    # ── AWS Lambda ─────────────────────────────────────────────────────────────
    # 13. Lambda error spike + deploy event — most specific AWS compound rule
    (
        frozenset({"lambda_error_rate_high", "deploy_event"}),
        True,
        {
            "root_cause": "Lambda error rate spiked after a recent deployment — "
            "the new function code or configuration is likely causing failures.",
            "failure_class": "bad_deploy",
            "healing_level": 3,
            "runbook_id": "runbook_lambda_error_spike_v1",
            "confidence": 0.88,
            "reasoning": "Lambda error rate correlated with deploy event is the canonical "
            "'bad deploy' pattern for serverless. Alias rollback is the "
            "first safe remediation.",
        },
    ),
    # 14. Lambda error spike alone (no deploy correlated)
    (
        frozenset({"lambda_error_rate_high"}),
        False,
        {
            "root_cause": "Lambda function error rate elevated with no correlated deployment — "
            "possible upstream dependency failure, config change, or transient AWS issue.",
            "failure_class": "unknown",
            "healing_level": 0,
            "runbook_id": "runbook_lambda_error_spike_v1",
            "confidence": 0.55,
            "reasoning": "Error rate elevated but no deploy detected. Alert only — "
            "more signals needed before autonomous action.",
        },
    ),
    # 15. Lambda throttle spike
    (
        frozenset({"lambda_throttle_spike"}),
        False,
        {
            "root_cause": "Lambda function is being throttled — reserved or account-level "
            "concurrency limit has been reached.",
            "failure_class": "resource_exhaustion",
            "healing_level": 2,
            "runbook_id": "runbook_lambda_throttle_v1",
            "confidence": 0.82,
            "reasoning": "Lambda throttles indicate concurrency saturation. "
            "Increasing reserved concurrency is a safe, bounded L2 action.",
        },
    ),
    # 16. Lambda timeout
    (
        frozenset({"lambda_timeout"}),
        False,
        {
            "root_cause": "Lambda function duration approaching configured timeout — "
            "processing is slower than expected, possibly due to a slow "
            "downstream service or increased payload size.",
            "failure_class": "resource_exhaustion",
            "healing_level": 2,
            "runbook_id": "runbook_lambda_timeout_v1",
            "confidence": 0.75,
            "reasoning": "Duration > 80% of timeout is a clear precursor to timeout errors. "
            "Increasing timeout is bounded and safe.",
        },
    ),
    # 17. Lambda OOM
    (
        frozenset({"lambda_oom"}),
        False,
        {
            "root_cause": "Lambda function terminated by the runtime due to memory exhaustion. "
            "Memory limit must be increased or a memory leak must be fixed.",
            "failure_class": "resource_exhaustion",
            "healing_level": 2,
            "runbook_id": "runbook_lambda_oom_v1",
            "confidence": 0.88,
            "reasoning": "OOM is a deterministic signal — runtime exited with memory error. "
            "Increasing memory 50% is the standard first response.",
        },
    ),
    # AWS SQS
    # 18. SQS DLQ depth high
    (
        frozenset({"sqs_dlq_depth_high"}),
        False,
        {
            "root_cause": "Messages accumulating in the dead-letter queue — the consumer Lambda "
            "is failing to process messages (errors or timeouts).",
            "failure_class": "dependency_failure",
            "healing_level": 2,
            "runbook_id": "runbook_sqs_dlq_v1",
            "confidence": 0.78,
            "reasoning": "DLQ depth growth indicates consumer failures. Alert + conditional "
            "replay is the standard SQS incident response.",
        },
    ),
    # 19. SQS queue depth high
    (
        frozenset({"sqs_queue_depth_high"}),
        False,
        {
            "root_cause": "SQS source queue depth growing without consumption — "
            "consumer Lambda may be scaled down, throttled, or failing.",
            "failure_class": "resource_exhaustion",
            "healing_level": 0,
            "runbook_id": None,
            "confidence": 0.60,
            "reasoning": "High queue depth alone is ambiguous — consumer could be paused "
            "or scaling behind. Alert to investigate.",
        },
    ),
    # AWS DynamoDB
    # 20. DynamoDB read throttle
    (
        frozenset({"dynamo_throttle_read"}),
        False,
        {
            "root_cause": "DynamoDB read capacity exhausted — table is receiving more "
            "read requests than provisioned RCU can handle.",
            "failure_class": "resource_exhaustion",
            "healing_level": 0,
            "runbook_id": "runbook_dynamo_throttle_v1",
            "confidence": 0.80,
            "reasoning": "DynamoDB throttles require capacity planning — autonomous "
            "capacity increases risk cost overruns. Alert only.",
        },
    ),
    # 21. DynamoDB write throttle
    (
        frozenset({"dynamo_throttle_write"}),
        False,
        {
            "root_cause": "DynamoDB write capacity exhausted — table is receiving more "
            "write requests than provisioned WCU can handle.",
            "failure_class": "resource_exhaustion",
            "healing_level": 0,
            "runbook_id": "runbook_dynamo_throttle_v1",
            "confidence": 0.80,
            "reasoning": "DynamoDB write throttles require capacity planning. Alert only.",
        },
    ),
    # 22. DynamoDB system error
    (
        frozenset({"dynamo_system_error"}),
        False,
        {
            "root_cause": "DynamoDB system errors detected — possible AWS infrastructure issue "
            "or table is in an error state.",
            "failure_class": "dependency_failure",
            "healing_level": 0,
            "runbook_id": "runbook_dynamo_throttle_v1",
            "confidence": 0.85,
            "reasoning": "DynamoDB SystemErrors are AWS-side issues. Alert for manual "
            "investigation and potential AWS Support case.",
        },
    ),
    # AWS API Gateway
    # 23. API GW 5XX spike + Lambda error rate
    (
        frozenset({"apigw_5xx_spike", "lambda_error_rate_high"}),
        True,
        {
            "root_cause": "API Gateway 5XX errors correlated with Lambda backend failures — "
            "the backend function is returning errors causing the gateway to surface 500s.",
            "failure_class": "bad_deploy",
            "healing_level": 3,
            "runbook_id": "runbook_lambda_error_spike_v1",
            "confidence": 0.87,
            "reasoning": "5XX at gateway + Lambda errors is a clear cascading signal. "
            "Fixing the Lambda (rollback or memory increase) resolves both.",
        },
    ),
    # 24. API GW 5XX spike alone
    (
        frozenset({"apigw_5xx_spike"}),
        False,
        {
            "root_cause": "API Gateway returning 5XX errors — backend Lambda or integration "
            "is unhealthy or unavailable.",
            "failure_class": "unknown",
            "healing_level": 0,
            "runbook_id": None,
            "confidence": 0.60,
            "reasoning": "5XX without correlated Lambda signal could be integration timeout "
            "or misconfiguration. Alert and investigate.",
        },
    ),
]

def _rule_based_rca(cluster: IncidentCluster) -> RCAResult:
    """
    Deterministic fallback RCA — evaluates rule table against cluster signal types.
    Rules are priority-ordered (most specific first).
    """
    signal_types = cluster.signal_types  # Set[str]

    for required, partial_ok, tmpl in _RULES:
        if partial_ok:
            matched = bool(required & signal_types)  # ANY required signal present
        else:
            matched = required.issubset(signal_types)  # ALL required signals present

        if matched:
            return RCAResult(
                root_cause=tmpl["root_cause"],
                failure_class=tmpl["failure_class"],
                healing_level=tmpl["healing_level"],
                runbook_id=tmpl.get("runbook_id"),
                confidence=tmpl["confidence"],
                reasoning=tmpl["reasoning"],
                source="rule_based",
            )

    # Default — cannot determine
    return RCAResult(
        root_cause="Unable to determine root cause from available signals.",
        failure_class="unknown",
        healing_level=0,
        runbook_id=None,
        confidence=0.30,
        reasoning="No matching rule found — alerting only.",
        source="rule_based",
    )


# Gemini prompt builder
def _build_gemini_prompt(
    cluster: IncidentCluster, historical_runbook: str | None = None
) -> str:
    base = cluster.to_llm_context()
    if historical_runbook:
        base += f"\n\nHistorical Note: In the past, the runbook '{historical_runbook}' was successfully used for this exact combination of incident signals. Strongly consider it if it fits the current context."
    return base

# RCA Engine
class RCAEngine:
    """
    Root Cause Analysis engine.

    Primary:  Any configured LLM provider (Gemini / OpenAI / Anthropic / none)
              Selected via NEXUS_LLM_PROVIDER env var.
    Fallback: Deterministic rule table (always available, zero latency)

    Args:
        api_key:      API key for the LLM provider. Falls back to env vars.
        model:        Model name override. Falls back to NEXUS_LLM_MODEL (default: gemini).
        timeout_s:    Maximum seconds to wait for LLM response (default: 10).
        use_fallback: If True (default), use rule-based fallback on LLM errors.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gemini-3.1-flash-lite",
        timeout_s: float = 10.0,
        use_fallback: bool = True,
        knowledge_base: Any = None,
    ):
        self._timeout_s = float(os.getenv("NEXUS_RCA_TIMEOUT_S", str(timeout_s)))
        self._use_fallback = use_fallback
        self._knowledge_base = knowledge_base
        self._llm_calls = 0
        self._llm_errors = 0

        # Resolve provider once at init time
        from nexus.reasoning.llm_provider import get_llm_provider

        self._provider = get_llm_provider(api_key=api_key or None, model=model or None)

    async def analyze(self, cluster: IncidentCluster) -> RCAResult:
        """
        Analyze an IncidentCluster and return an RCAResult.

        Tries the configured LLM first; falls back to rule-based on any failure.
        Never raises — always returns a valid RCAResult.
        """
        historical_runbook = None
        if self._knowledge_base:
            historical_runbook = (
                await self._knowledge_base.get_best_runbook_for_pattern(
                    cluster.signal_types
                )
            )

        if self._provider.is_available():
            try:
                prompt = _build_gemini_prompt(
                    cluster, historical_runbook=historical_runbook
                )
                raw_text = await asyncio.wait_for(
                    self._provider.complete(prompt),
                    timeout=self._timeout_s,
                )
                self._llm_calls += 1
                result = self._parse_response(raw_text)
                if result:
                    # Update source field with actual provider name
                    result.source = self._provider.name
                    logger.info(
                        f"[RCAEngine] {self._provider.name.upper()} RCA: "
                        f"class={result.failure_class} L{result.healing_level} "
                        f"conf={result.confidence:.2f} runbook={result.runbook_id}"
                    )
                    return result
            except (asyncio.TimeoutError, Exception) as exc:
                self._llm_errors += 1
                logger.warning(
                    f"[RCAEngine] {self._provider.name} error: {exc} — trying fallback"
                )

                # OpenAI Fallback
                if self._provider.name == "gemini" and os.getenv(
                    "NEXUS_OPENAI_API_KEY"
                ):
                    try:
                        logger.info("[RCAEngine] Falling back to OpenAI provider")
                        from nexus.reasoning.llm_provider import OpenAIProvider

                        openai_provider = OpenAIProvider(
                            api_key=os.getenv("NEXUS_OPENAI_API_KEY")
                        )
                        prompt = _build_gemini_prompt(
                            cluster, historical_runbook=historical_runbook
                        )
                        raw_text = await asyncio.wait_for(
                            openai_provider.complete(prompt),
                            timeout=self._timeout_s,
                        )
                        self._llm_calls += 1
                        result = self._parse_response(raw_text)
                        if result:
                            result.source = "openai"
                            logger.info(
                                f"[RCAEngine] OPENAI RCA: "
                                f"class={result.failure_class} L{result.healing_level} "
                                f"conf={result.confidence:.2f} runbook={result.runbook_id}"
                            )
                            return result
                    except Exception as fallback_exc:
                        self._llm_errors += 1
                        logger.warning(
                            f"[RCAEngine] OpenAI fallback error: {fallback_exc} — using rule-based fallback"
                        )

        # Rule-based fallback
        result = _rule_based_rca(cluster)
        logger.info(
            f"[RCAEngine] Rule-based RCA: class={result.failure_class} "
            f"L{result.healing_level} conf={result.confidence:.2f} "
            f"runbook={result.runbook_id}"
        )
        return result

    def _parse_response(self, raw: str) -> RCAResult | None:
        """Parse LLM JSON response into an RCAResult."""
        import re as _re
        clean = _re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()

        try:
            data = json.loads(clean)
        except json.JSONDecodeError as exc:
            first = clean.find("{")
            last = clean.rfind("}")
            if first >= 0 and last > first:
                snippet = clean[first:last + 1]
                try:
                    data = json.loads(snippet)
                except json.JSONDecodeError:
                    logger.warning(f"[RCAEngine] JSON parse failed: {exc}\nRaw: {raw[:200]}")
                    return None
            else:
                logger.warning(f"[RCAEngine] JSON parse failed: {exc}\nRaw: {raw[:200]}")
                return None

        try:
            return RCAResult(
                root_cause=str(data.get("root_cause", "Unknown")),
                failure_class=str(data.get("failure_class", "unknown")),
                healing_level=int(data.get("healing_level", 0)),
                runbook_id=data.get("runbook_id") or None,
                confidence=float(data.get("confidence", 0.5)),
                reasoning=str(data.get("reasoning", "")),
                source="llm",
                actions_to_avoid=list(data.get("actions_to_avoid", [])),
            )
        except (TypeError, ValueError) as exc:
            logger.warning(f"[RCAEngine] Response schema invalid: {exc}")
            return None

    @property
    def stats(self) -> dict:
        return {
            "llm_calls": self._llm_calls,
            "llm_errors": self._llm_errors,
            "provider": self._provider.name,
            "model": self._provider.model,
            "has_api_key": self._provider.is_available(),
        }
