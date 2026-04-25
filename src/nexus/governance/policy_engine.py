"""
NEXUS Policy Engine
====================
Evaluates healing actions against an OPA (Open Policy Agent) policy —
or a built-in Python fallback if OPA is unavailable.

OPA is the authoritative policy source. The fallback exists so the system
degrades gracefully in dev / test environments without OPA running.

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
        source               = "opa" | "fallback"
    )
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Policy Decision
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class PolicyDecision:
    allowed:           bool
    requires_approval: bool = False
    deny_reasons:      List[str] = field(default_factory=list)
    source:            str = "unknown"   # "opa" | "fallback"

    @property
    def denied(self) -> bool:
        return not self.allowed

    def __str__(self) -> str:
        status = "ALLOW" if self.allowed else "DENY"
        if self.requires_approval:
            status = "PENDING_APPROVAL"
        reasons = f" [{', '.join(self.deny_reasons)}]" if self.deny_reasons else ""
        return f"PolicyDecision({status}{reasons} source={self.source})"


# ──────────────────────────────────────────────────────────────────────────────
# Fallback policy (pure Python — mirrors nexus_policies.rego)
# ──────────────────────────────────────────────────────────────────────────────

# Action type allowlists per healing level
_L0_ALLOWED = {"emit_alert", "patch_annotation"}
_L1_ALLOWED = _L0_ALLOWED | {"restart_pod", "flush_coredns_cache"}
_L2_ALLOWED = _L1_ALLOWED | {"scale_deployment"}
_L3_ALLOWED = _L2_ALLOWED | {"kubectl_rollout_undo", "http_webhook"}

_LEVEL_LISTS = {0: _L0_ALLOWED, 1: _L1_ALLOWED, 2: _L2_ALLOWED, 3: _L3_ALLOWED}

_CLUSTER_WIDE_BLAST_RADIUS = {"cluster_wide"}


def _fallback_evaluate(
    action_type: str,
    healing_level: int,
    blast_radius: str,
    in_cooldown: bool,
    governance_cb_open: bool,
    confidence: float,
    human_approved: bool,
    override_blast_radius: bool,
) -> PolicyDecision:
    """
    Pure Python policy evaluation — equivalent to nexus_policies.rego.
    Called when OPA is unreachable.
    """
    deny_reasons: List[str] = []

    # Governance circuit breaker
    if governance_cb_open:
        deny_reasons.append("governance_circuit_breaker_open_stop_autonomous_healing")

    # Cooldown
    if in_cooldown:
        deny_reasons.append("action_in_cooldown")

    # Action type allowlist
    allowed_types = _LEVEL_LISTS.get(healing_level, set())
    if action_type not in allowed_types:
        deny_reasons.append(f"action_type_not_in_allowlist_for_level_{healing_level}")

    # Blast radius (L2+ only)
    if healing_level >= 2 and blast_radius in _CLUSTER_WIDE_BLAST_RADIUS and not override_blast_radius:
        deny_reasons.append("blast_radius_cluster_wide_not_allowed_without_override")

    # L3 confidence gate
    requires_approval = False
    if healing_level == 3 and confidence < 0.85 and not human_approved:
        deny_reasons.append("l3_action_requires_confidence_0.85_or_human_approval")
        requires_approval = True

    if deny_reasons and not (requires_approval and len(deny_reasons) == 1):
        # Blocked unless the only denial is pending human approval
        if not (human_approved and requires_approval):
            return PolicyDecision(
                allowed=False,
                requires_approval=requires_approval,
                deny_reasons=deny_reasons,
                source="fallback",
            )

    # L3 + human_approved bypasses confidence gate
    if healing_level == 3 and requires_approval and human_approved:
        return PolicyDecision(allowed=True, source="fallback")

    if deny_reasons:
        return PolicyDecision(
            allowed=False,
            requires_approval=requires_approval,
            deny_reasons=deny_reasons,
            source="fallback",
        )

    return PolicyDecision(allowed=True, source="fallback")


# ──────────────────────────────────────────────────────────────────────────────
# OPA HTTP Client
# ──────────────────────────────────────────────────────────────────────────────

class PolicyEngine:
    """
    Evaluates healing actions against OPA policies.

    Automatically falls back to the built-in Python policy engine if OPA
    is unreachable (timeouts, connection refused, etc.).

    Args:
        opa_url:       OPA HTTP API URL (default http://localhost:8181).
        http_timeout:  Request timeout in seconds (default 2.0 — must be fast).
        fallback_on_error: If True (default), use Python fallback on OPA errors.
    """

    def __init__(
        self,
        opa_url: str = "http://localhost:8181",
        http_timeout: float = 2.0,
        fallback_on_error: bool = True,
    ):
        self._opa_url         = opa_url.rstrip("/")
        self._http_timeout    = http_timeout
        self._fallback        = fallback_on_error
        self._opa_available   = True    # Optimistic; flipped on first failure

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

        First queries OPA; falls back to Python policy if OPA errors.
        """
        input_doc = {
            "input": {
                "action": {
                    "type":                  action_type,
                    "level":                 healing_level,
                    "blast_radius":          blast_radius,
                    "in_cooldown":           in_cooldown,
                    "governance_cb_open":    governance_cb_open,
                    "confidence":            confidence,
                    "human_approved":        human_approved,
                    "override_blast_radius": override_blast_radius,
                }
            }
        }

        if self._opa_available:
            try:
                decision = await self._query_opa(input_doc)
                return decision
            except Exception as exc:
                self._opa_available = False
                logger.warning(f"[PolicyEngine] OPA error ({exc}) — switching to fallback")

        if not self._fallback:
            # Hard fail if fallback disabled (production strict mode)
            return PolicyDecision(
                allowed=False,
                deny_reasons=["opa_unavailable_and_fallback_disabled"],
                source="error",
            )

        return _fallback_evaluate(
            action_type=action_type,
            healing_level=healing_level,
            blast_radius=blast_radius,
            in_cooldown=in_cooldown,
            governance_cb_open=governance_cb_open,
            confidence=confidence,
            human_approved=human_approved,
            override_blast_radius=override_blast_radius,
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
            deny_reasons: List[str] = []
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
