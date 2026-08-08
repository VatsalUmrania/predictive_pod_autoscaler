"""
NEXUS Policy Engine
====================
Evaluates healing actions against OPA (Open Policy Agent).

OPA is the single, authoritative policy source and a hard dependency — there
is no built-in Python fallback. If OPA is unreachable, the action is DENIED
(fail-closed) and the source is reported as "error" so the gap is visible in
the audit trail. Only the ``LLM_TOOL_LEVEL`` mapping lives in Python (it is
input-shaping data, not policy).

OPA REST API:
    POST /v1/data/nexus/allow_action
    POST /v1/data/nexus/deny_reasons
    GET  /health

Input document sent to OPA:
    {
        "input": {
            "action": {
                "type":               "restart_pod",
                "level":              1,
                "blast_radius":       "single_pod",
                "in_cooldown":        false,
                "governance_cb_open": false,
                "confidence":         0.92,
                "human_approved":     false,
                "override_blast_radius": false
            }
        }
    }

Output:
    PolicyDecision(
        allowed              = True,
        requires_approval    = False,
        deny_reasons         = [],
        source               = "opa" | "error"
    )
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

# Policy Decision
@dataclass
class PolicyDecision:
    allowed: bool
    requires_approval: bool = False
    deny_reasons: list[str] = field(default_factory=list)
    source: str = "unknown"  # "opa" | "error"

    @property
    def denied(self) -> bool:
        return not self.allowed

    def __str__(self) -> str:
        status = "ALLOW" if self.allowed else "DENY"
        if self.requires_approval:
            status = "PENDING_APPROVAL"
        reasons = f" [{', '.join(self.deny_reasons)}]" if self.deny_reasons else ""
        return f"PolicyDecision({status}{reasons} source={self.source})"


# LLM-proposed tool → (healing_level, blast_radius). The level is the tool's
# NOMINAL blast — a cordon is L3 regardless of confidence; confidence only
# decides auto-run vs human-approval, not the action's level. Used by
# LLMOrchestrator (staging) and RunbookExecutor (synthesis) to score an LLM
# proposal at its real blast. This is INPUT SHAPING, not policy — the actual
# allow/deny decision is made by OPA in nexus_policies.rego.
LLM_TOOL_LEVEL: dict[str, tuple[int, str]] = {
    "restart_deployment": (1, "single_deployment"),
    "scale_resource": (2, "single_deployment"),
    "rollback_deployment": (3, "single_deployment"),
    "patch_configmap": (3, "single_deployment"),
    "cordon_node": (3, "cluster_wide"),
    "drain_node": (3, "cluster_wide"),
}


# OPA HTTP Client
class PolicyEngine:
    """
    Evaluates healing actions against OPA.

    OPA is a hard dependency: if it is unreachable, evaluate() returns a
    deny-closed PolicyDecision (source="error"). There is no Python fallback —
    keep policy solely in nexus_policies.rego.

    Args:
        opa_url:       OPA HTTP API URL (default http://localhost:8181).
        http_timeout:  Request timeout in seconds (default 2.0 — must be fast).
    """

    def __init__(
        self,
        opa_url: str = "http://localhost:8181",
        http_timeout: float = 2.0,
    ):
        self._opa_url = opa_url.rstrip("/")
        self._http_timeout = http_timeout
        self._opa_available = True  # Optimistic; flipped off on first failure.
        self._opa_retry_after: float = 0.0
        self._opa_retry_cooldown_s = 60.0

    async def is_healthy(self) -> bool:
        """Check if OPA is reachable."""
        try:
            async with httpx.AsyncClient(timeout=self._http_timeout) as client:
                resp = await client.get(f"{self._opa_url}/health")
                self._opa_available = resp.status_code == 200
                return self._opa_available
        except Exception:
            self._opa_available = False
            return False

    async def evaluate(
        self,
        action_type: str,
        healing_level: int,
        blast_radius: str = "unknown",
        in_cooldown: bool = False,
        governance_cb_open: bool = False,
        confidence: float = 1.0,
        human_approved: bool = False,
        override_blast_radius: bool = False,
    ) -> PolicyDecision:
        """
        Evaluate whether a healing action is permitted.

        Queries OPA; returns a DENY-closed decision if OPA is unavailable.
        """
        input_doc = {
            "input": {
                "action": {
                    "type": action_type,
                    "level": healing_level,
                    "blast_radius": blast_radius,
                    "in_cooldown": in_cooldown,
                    "governance_cb_open": governance_cb_open,
                    "confidence": confidence,
                    "human_approved": human_approved,
                    "override_blast_radius": override_blast_radius,
                }
            }
        }

        import time
        if not self._opa_available and time.monotonic() >= self._opa_retry_after:
            self._opa_available = True

        if self._opa_available:
            try:
                return await self._query_opa(input_doc)
            except Exception as exc:
                self._opa_available = False
                self._opa_retry_after = time.monotonic() + self._opa_retry_cooldown_s
                logger.warning(
                    f"[PolicyEngine] OPA error ({exc}) — denying (fail-closed), "
                    f"will retry in {self._opa_retry_cooldown_s}s"
                )

        # Fail-closed when OPA is unavailable — no Python fallback.
        return PolicyDecision(
            allowed=False,
            deny_reasons=["opa_unavailable_deny_closed"],
            source="error",
        )

    async def _query_opa(self, input_doc: dict) -> PolicyDecision:
        async with httpx.AsyncClient(timeout=self._http_timeout) as client:
            # Query allow_action rule
            allow_resp = await client.post(
                f"{self._opa_url}/v1/data/nexus/allow_action",
                json=input_doc,
            )
            allow_resp.raise_for_status()
            allowed = bool(allow_resp.json().get("result", False))

            # Query deny_reasons rule for diagnostic info
            deny_reasons: list[str] = []
            deny_resp = await client.post(
                f"{self._opa_url}/v1/data/nexus/deny_reasons",
                json=input_doc,
            )
            if deny_resp.status_code == 200:
                raw = deny_resp.json().get("result", [])
                deny_reasons = list(raw) if raw else []

            # Query requires_human_approval rule
            requires_approval = False
            appr_resp = await client.post(
                f"{self._opa_url}/v1/data/nexus/requires_human_approval",
                json=input_doc,
            )
            if appr_resp.status_code == 200:
                requires_approval = bool(appr_resp.json().get("result", False))

            return PolicyDecision(
                allowed=allowed,
                requires_approval=requires_approval,
                deny_reasons=deny_reasons,
                source="opa",
            )

    @property
    def opa_available(self) -> bool:
        return self._opa_available
