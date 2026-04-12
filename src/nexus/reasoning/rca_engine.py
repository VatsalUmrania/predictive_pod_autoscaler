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
    NEXUS_GEMINI_API_KEY   — required for Gemini; fallback used if absent
    NEXUS_GEMINI_MODEL     — model name (default: gemini-1.5-flash)
    NEXUS_RCA_TIMEOUT_S    — Gemini request timeout in seconds (default: 10)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from nexus.reasoning.incident_cluster import IncidentCluster

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# RCA Result
# ──────────────────────────────────────────────────────────────────────────────

VALID_FAILURE_CLASSES = frozenset({
    "bad_deploy",
    "resource_exhaustion",
    "dependency_failure",
    "config_error",
    "cascading_failure",
    "unknown",
})

VALID_HEALING_LEVELS = (0, 1, 2, 3)


@dataclass
class RCAResult:
    """The output of the RCA Engine for one IncidentCluster."""
    root_cause:    str
    failure_class: str
    healing_level: int
    runbook_id:    Optional[str]
    confidence:    float
    reasoning:     str
    source:        str   # "gemini" | "rule_based"
    actions_to_avoid: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Clamp and validate
        self.confidence    = max(0.0, min(1.0, self.confidence))
        self.healing_level = max(0, min(3, self.healing_level))
        if self.failure_class not in VALID_FAILURE_CLASSES:
            self.failure_class = "unknown"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "root_cause":        self.root_cause,
            "failure_class":     self.failure_class,
            "healing_level":     self.healing_level,
            "runbook_id":        self.runbook_id,
            "confidence":        round(self.confidence, 3),
            "reasoning":         self.reasoning,
            "source":            self.source,
            "actions_to_avoid":  self.actions_to_avoid,
        }

    def __str__(self) -> str:
        return (
            f"RCAResult(class={self.failure_class}, L{self.healing_level}, "
            f"confidence={self.confidence:.2f}, runbook={self.runbook_id}, "
            f"src={self.source})"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Rule-based fallback
# ──────────────────────────────────────────────────────────────────────────────

# Rules are (required_signals, partial_ok, result_template)
# required_signals ⊆ cluster.signal_types for the rule to match.
# partial_ok=True means ANY signal in the set triggers the rule (OR match).
# partial_ok=False means ALL signals required (AND match).
# Rules are evaluated top-to-bottom; first match wins.

_RULES: List[Tuple[FrozenSet[str], bool, Dict[str, Any]]] = [

    # ── Highest confidence first ───────────────────────────────────────────────

    # 1. ENV contract violation — deterministic, always L0 block
    (
        frozenset({"env_contract_violation"}), False,
        {
            "root_cause":    "Required environment variables are missing from the deployment. "
                             "The container will fail to start without them.",
            "failure_class": "config_error",
            "healing_level": 0,
            "runbook_id":    "runbook_missing_env_key_v1",
            "confidence":    0.97,
            "reasoning":     "ENV_CONTRACT_VIOLATION is a deterministic signal — "
                             "the AST scanner found required keys absent from secrets/env.",
        },
    ),

    # 2. Secret accidentally committed — security incident, L0 (alert immediately)
    (
        frozenset({"secret_committed"}), False,
        {
            "root_cause":    "A potential secret or credential was detected in a recent git commit.",
            "failure_class": "config_error",
            "healing_level": 0,
            "runbook_id":    None,
            "confidence":    0.90,
            "reasoning":     "SECRET_COMMITTED detected in diff — "
                             "require immediate manual revocation and secret rotation.",
        },
    ),

    # 3. OOMKilled — clearest resource signal
    (
        frozenset({"pod_oomkilled"}), False,
        {
            "root_cause":    "Pod terminated by the OOM killer — memory limit exceeded. "
                             "Container needs higher memory limits or there is a memory leak.",
            "failure_class": "resource_exhaustion",
            "healing_level": 1,
            "runbook_id":    "runbook_pod_crashloop_v1",
            "confidence":    0.90,
            "reasoning":     "OOMKilled is a deterministic K8s signal. "
                             "VPA hint + pod restart is the appropriate first response.",
        },
    ),

    # 4. Pod crash + deploy event (most specific compound rule)
    (
        frozenset({"pod_crashloop", "deploy_event"}), True,
        {
            "root_cause":    "Pod crash loop correlated with a recent deployment — "
                             "the new image or config change is likely causing startup failures.",
            "failure_class": "bad_deploy",
            "healing_level": 2,
            "runbook_id":    "runbook_high_error_rate_post_deploy_v1",
            "confidence":    0.83,
            "reasoning":     "CrashLoopBackOff coinciding with a deploy event "
                             "is the canonical 'bad deploy' pattern. "
                             "Canary halt + rollout undo is the appropriate response.",
        },
    ),

    # 5. High error rate + deploy event
    (
        frozenset({"high_error_rate", "deploy_event"}), True,
        {
            "root_cause":    "HTTP error rate spike starting after a recent deployment — "
                             "regression in the new code version.",
            "failure_class": "bad_deploy",
            "healing_level": 2,
            "runbook_id":    "runbook_high_error_rate_post_deploy_v1",
            "confidence":    0.82,
            "reasoning":     "Error rate correlated with deploy time is a strong "
                             "bad-deploy signal. Halting canary traffic protects users.",
        },
    ),

    # 6. Pod crash alone
    (
        frozenset({"pod_crashloop"}), False,
        {
            "root_cause":    "Pod repeatedly crashing on startup — likely due to misconfiguration, "
                             "missing dependency, or application bug.",
            "failure_class": "bad_deploy",
            "healing_level": 1,
            "runbook_id":    "runbook_pod_crashloop_v1",
            "confidence":    0.82,
            "reasoning":     "CrashLoopBackOff is a clear K8s failure signal. "
                             "Pod restart resets the exponential backoff timer.",
        },
    ),

    # 7. DB connection exhaustion
    (
        frozenset({"db_connection_exhaustion"}), False,
        {
            "root_cause":    "Database connection pool is exhausted — connections are not "
                             "being released or traffic spike exceeded pool capacity.",
            "failure_class": "resource_exhaustion",
            "healing_level": 2,
            "runbook_id":    "runbook_db_connection_exhaustion_v1",
            "confidence":    0.80,
            "reasoning":     "DB connection utilization at threshold is a resource "
                             "exhaustion signal. Alerting + annotation allows DBAs to investigate.",
        },
    ),

    # 8. DNS resolution failure
    (
        frozenset({"dns_resolution_failure"}), False,
        {
            "root_cause":    "DNS resolution failures detected — CoreDNS may be "
                             "overloaded or its cache is corrupted.",
            "failure_class": "dependency_failure",
            "healing_level": 1,
            "runbook_id":    "runbook_dns_resolution_failure_v1",
            "confidence":    0.77,
            "reasoning":     "DNS failures affect all services in the namespace. "
                             "CoreDNS cache flush is safe and typically resolves stale records.",
        },
    ),

    # 9. HPA maxed out
    (
        frozenset({"hpa_maxed"}), False,
        {
            "root_cause":    "HPA has reached maximum replica count — cluster cannot "
                             "auto-scale further to handle the current load.",
            "failure_class": "resource_exhaustion",
            "healing_level": 0,
            "runbook_id":    None,
            "confidence":    0.72,
            "reasoning":     "HPA saturation is a capacity ceiling problem. "
                             "Autonomous healing cannot increase cluster capacity — escalate.",
        },
    ),

    # 10. Deployment degraded
    (
        frozenset({"deployment_degraded"}), False,
        {
            "root_cause":    "Deployment has fewer available replicas than desired — "
                             "pods failing to start or being evicted.",
            "failure_class": "bad_deploy",
            "healing_level": 1,
            "runbook_id":    "runbook_pod_crashloop_v1",
            "confidence":    0.68,
            "reasoning":     "Degraded deployment often results from pod startup failures. "
                             "Pod restart with logging is the first investigation step.",
        },
    ),

    # 11. High error rate alone (no deploy correlated)
    (
        frozenset({"high_error_rate"}), False,
        {
            "root_cause":    "Elevated HTTP error rate with no correlated deployment — "
                             "possible upstream dependency failure or transient spike.",
            "failure_class": "unknown",
            "healing_level": 0,
            "runbook_id":    None,
            "confidence":    0.50,
            "reasoning":     "Error rate elevated but no deploy detected. "
                             "Alerting only — more signals needed before autonomous action.",
        },
    ),

    # 12. Rollout stuck
    (
        frozenset({"rollout_stuck"}), False,
        {
            "root_cause":    "Kubernetes deployment rollout has exceeded its progress deadline — "
                             "new pods are not becoming ready.",
            "failure_class": "bad_deploy",
            "healing_level": 3,
            "runbook_id":    None,     # Rollout undo is available but requires high confidence
            "confidence":    0.75,
            "reasoning":     "ProgressDeadlineExceeded is a K8s rollout failure signal. "
                             "Manual rollback evaluation is recommended at L3.",
        },
    ),
]


def _rule_based_rca(cluster: IncidentCluster) -> RCAResult:
    """
    Deterministic fallback RCA — evaluates rule table against cluster signal types.
    Rules are priority-ordered (most specific first).
    """
    signal_types = cluster.signal_types   # Set[str]

    for required, partial_ok, tmpl in _RULES:
        if partial_ok:
            matched = bool(required & signal_types)    # ANY required signal present
        else:
            matched = required.issubset(signal_types)  # ALL required signals present

        if matched:
            return RCAResult(
                root_cause    = tmpl["root_cause"],
                failure_class = tmpl["failure_class"],
                healing_level = tmpl["healing_level"],
                runbook_id    = tmpl.get("runbook_id"),
                confidence    = tmpl["confidence"],
                reasoning     = tmpl["reasoning"],
                source        = "rule_based",
            )

    # Default — cannot determine
    return RCAResult(
        root_cause    = "Unable to determine root cause from available signals.",
        failure_class = "unknown",
        healing_level = 0,
        runbook_id    = None,
        confidence    = 0.30,
        reasoning     = "No matching rule found — alerting only.",
        source        = "rule_based",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Gemini prompt builder
# ──────────────────────────────────────────────────────────────────────────────

_SYSTEM_INSTRUCTION = """\
You are NEXUS, an autonomous cloud infrastructure Root Cause Analysis (RCA) system.
You receive correlated incident signals from multiple domain agents monitoring a Kubernetes cluster.

Your task: analyze the signals and determine the most likely root cause.

Rules:
- Be specific and technical — not generic filler text
- Prefer the simplest hypothesis that explains all signals (Occam's razor)
- Use the available runbook list to constrain your action recommendation
- healing_level 0 = alert only, 1 = no-regret (pod restart), 2 = bounded mitigation (scale/canary halt), 3 = significant change (rollout undo)
- confidence 0.0-1.0 — be conservative; prefer 0.5-0.8 range unless signals are deterministic
- If multiple explanations are equally plausible, choose the more conservative (lower healing_level)

Available runbooks:
- runbook_pod_crashloop_v1 (L1): Restart pod + VPA hint
- runbook_high_error_rate_post_deploy_v1 (L2): Halt canary + alert
- runbook_missing_env_key_v1 (L0): Block deploy + alert
- runbook_dns_resolution_failure_v1 (L1): Flush CoreDNS cache + escalate
- runbook_db_connection_exhaustion_v1 (L2): Alert + annotate deployment

Respond ONLY with valid JSON. No markdown fences, no prose outside the JSON structure.

Required schema:
{
  "root_cause": "string — 1-2 sentences, specific technical cause",
  "failure_class": "one of: bad_deploy | resource_exhaustion | dependency_failure | config_error | cascading_failure | unknown",
  "healing_level": 0,
  "runbook_id": "exact runbook ID from the list above, or null",
  "confidence": 0.0,
  "reasoning": "string — 2-3 sentences of chain-of-thought",
  "actions_to_avoid": ["list of action types that would make this worse"]
}\
"""


def _build_gemini_prompt(cluster: IncidentCluster) -> str:
    return cluster.to_llm_context()


# ──────────────────────────────────────────────────────────────────────────────
# RCA Engine
# ──────────────────────────────────────────────────────────────────────────────

class RCAEngine:
    """
    Root Cause Analysis engine.

    Primary: Gemini 1.5 Flash (structured JSON output, low temperature)
    Fallback: Deterministic rule table (always available)

    Args:
        api_key:    Google AI API key. If None, reads NEXUS_GEMINI_API_KEY from env.
                    If still None/empty, falls back to rule-based.
        model:      Gemini model name (default: gemini-1.5-flash).
        timeout_s:  Maximum seconds to wait for Gemini response (default: 10).
        use_fallback: If True (default), use rule-based fallback on LLM errors.
    """

    def __init__(
        self,
        api_key:      Optional[str] = None,
        model:        str = "gemini-1.5-flash",
        timeout_s:    float = 10.0,
        use_fallback: bool = True,
    ):
        self._api_key     = api_key or os.getenv("NEXUS_GEMINI_API_KEY", "")
        self._model_name  = os.getenv("NEXUS_GEMINI_MODEL", model)
        self._timeout_s   = float(os.getenv("NEXUS_RCA_TIMEOUT_S", str(timeout_s)))
        self._use_fallback = use_fallback
        self._model       = None   # Lazy init on first call
        self._llm_calls   = 0
        self._llm_errors  = 0

    def _ensure_model(self) -> bool:
        """Initialize the Gemini model client. Returns False if SDK/key unavailable."""
        if self._model is not None:
            return True
        if not self._api_key:
            return False
        try:
            import google.generativeai as genai
            genai.configure(api_key=self._api_key)
            self._model = genai.GenerativeModel(
                model_name=self._model_name,
                system_instruction=_SYSTEM_INSTRUCTION,
                generation_config=genai.GenerationConfig(
                    temperature=0.2,
                    max_output_tokens=512,
                    response_mime_type="application/json",
                ),
            )
            logger.info(f"[RCAEngine] Gemini client initialized — model={self._model_name}")
            return True
        except ImportError:
            logger.warning(
                "[RCAEngine] google-generativeai not installed — "
                "install with: pip install google-generativeai"
            )
        except Exception as exc:
            logger.warning(f"[RCAEngine] Gemini init failed: {exc}")
        return False

    async def analyze(self, cluster: IncidentCluster) -> RCAResult:
        """
        Analyze an IncidentCluster and return an RCAResult.

        Tries Gemini first; falls back to rule-based on any failure.
        Never raises — always returns a valid RCAResult.
        """
        # Try Gemini
        if self._ensure_model():
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(self._call_gemini, cluster),
                    timeout=self._timeout_s,
                )
                if result:
                    logger.info(
                        f"[RCAEngine] Gemini RCA: class={result.failure_class} "
                        f"L{result.healing_level} conf={result.confidence:.2f} "
                        f"runbook={result.runbook_id}"
                    )
                    return result
            except asyncio.TimeoutError:
                self._llm_errors += 1
                logger.warning(
                    f"[RCAEngine] Gemini timeout ({self._timeout_s}s) — using fallback"
                )
            except Exception as exc:
                self._llm_errors += 1
                logger.warning(f"[RCAEngine] Gemini error: {exc} — using fallback")

        # Rule-based fallback
        result = _rule_based_rca(cluster)
        logger.info(
            f"[RCAEngine] Rule-based RCA: class={result.failure_class} "
            f"L{result.healing_level} conf={result.confidence:.2f} "
            f"runbook={result.runbook_id}"
        )
        return result

    def _call_gemini(self, cluster: IncidentCluster) -> Optional[RCAResult]:
        """Synchronous Gemini API call (run in thread via asyncio.to_thread)."""
        if not self._model:
            return None

        self._llm_calls += 1
        prompt   = _build_gemini_prompt(cluster)
        response = self._model.generate_content(prompt)
        raw_text = response.text.strip()

        return self._parse_response(raw_text)

    def _parse_response(self, raw: str) -> Optional[RCAResult]:
        """Parse Gemini's JSON response into an RCAResult."""
        # Strip markdown code fences if present (Gemini occasionally adds them)
        clean = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()

        try:
            data = json.loads(clean)
        except json.JSONDecodeError as exc:
            logger.warning(f"[RCAEngine] JSON parse failed: {exc}\nRaw: {raw[:200]}")
            return None

        try:
            return RCAResult(
                root_cause    = str(data.get("root_cause", "Unknown")),
                failure_class = str(data.get("failure_class", "unknown")),
                healing_level = int(data.get("healing_level", 0)),
                runbook_id    = data.get("runbook_id") or None,
                confidence    = float(data.get("confidence", 0.5)),
                reasoning     = str(data.get("reasoning", "")),
                source        = "gemini",
                actions_to_avoid = list(data.get("actions_to_avoid", [])),
            )
        except (TypeError, ValueError) as exc:
            logger.warning(f"[RCAEngine] Response schema invalid: {exc}")
            return None

    @property
    def stats(self) -> dict:
        return {
            "llm_calls":    self._llm_calls,
            "llm_errors":   self._llm_errors,
            "model":        self._model_name,
            "has_api_key":  bool(self._api_key),
        }
