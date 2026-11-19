"""
NEXUS Policy Engine
===================
Evaluates healing actions against OPA ABAC policies with deterministic Python
risk classifier overrides preventing LLM under-reporting of blast radius.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from nexus.governance.policies.policy_abac import (
    build_opa_input_for_action,
    evaluate_local_abac,
)
from nexus.governance.policies.policy_models import PolicyDecision

logger = logging.getLogger(__name__)


class PolicyEngine:
    """Evaluates healing actions against OPA ABAC policies."""

    def __init__(
        self,
        opa_url: str = "http://localhost:8181",
        http_timeout: float = 2.0,
        fallback_to_local: bool = False,
    ):
        self._opa_url = opa_url.rstrip("/")
        self._http_timeout = http_timeout
        self._fallback_to_local = fallback_to_local
        self._opa_available = True
        self._last_failure_time = 0.0
        self._retry_cooldown = 60.0

    @property
    def opa_available(self) -> bool:
        return self._opa_available

    async def is_healthy(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=self._http_timeout) as client:
                resp = await client.get(f"{self._opa_url}/health")
                return resp.status_code == 200
        except Exception:
            return False

    async def evaluate_action(self, input_doc: dict[str, Any]) -> dict[str, Any]:
        """Evaluate action dictionary against OPA or fallback."""
        if (
            not self._opa_available
            and (time.time() - self._last_failure_time) < self._retry_cooldown
        ):
            if self._fallback_to_local:
                dec, reasons = evaluate_local_abac(input_doc)
                return {"decision": dec, "deny_reasons": reasons, "source": "local"}
            return {
                "decision": "DENY",
                "deny_reasons": ["opa_unavailable_deny_closed"],
                "source": "error",
            }

        try:
            async with httpx.AsyncClient(timeout=self._http_timeout) as client:
                resp = await client.post(
                    f"{self._opa_url}/v1/data/nexus/governance",
                    json={"input": input_doc},
                )
                if resp.status_code == 200:
                    raw = resp.json().get("result")
                    if isinstance(raw, dict) and (
                        "decision" in raw or "allow" in raw or "allowed" in raw
                    ):
                        raw_dec = raw.get("decision")
                        if raw_dec:
                            dec = str(raw_dec)
                        else:
                            allow = raw.get("allow", raw.get("allowed", False))
                            req = raw.get(
                                "require_human_approval",
                                raw.get("requires_human_approval", False),
                            )
                            dec = (
                                "DENY"
                                if not allow
                                else ("REQUIRE_APPROVAL" if req else "ALLOW_AUTO")
                            )
                        return {
                            "decision": dec,
                            "deny_reasons": raw.get("deny_reasons", []),
                            "source": "opa",
                        }
                elif resp.status_code not in (400, 404):
                    resp.raise_for_status()
        except Exception as exc:
            if isinstance(exc, (httpx.RequestError, httpx.HTTPStatusError)):
                self._opa_available = False
                self._last_failure_time = time.time()
                if self._fallback_to_local:
                    dec, reasons = evaluate_local_abac(input_doc)
                    return {"decision": dec, "deny_reasons": reasons, "source": "local"}
                return {
                    "decision": "DENY",
                    "deny_reasons": ["opa_unavailable_deny_closed"],
                    "source": "error",
                }

        # Legacy endpoint fallback
        try:
            async with httpx.AsyncClient(timeout=self._http_timeout) as client:
                leg_doc = {"input": {"action": input_doc, "context": {}}}
                allow_resp = await client.post(
                    f"{self._opa_url}/v1/data/nexus/allow_action", json=leg_doc
                )
                allow_resp.raise_for_status()
                allowed = bool(allow_resp.json().get("result", False))
                deny_resp = await client.post(
                    f"{self._opa_url}/v1/data/nexus/deny_reasons", json=leg_doc
                )
                deny_reasons = (
                    list(deny_resp.json().get("result", []))
                    if deny_resp.status_code == 200
                    else []
                )
                appr_resp = await client.post(
                    f"{self._opa_url}/v1/data/nexus/requires_human_approval",
                    json=leg_doc,
                )
                req_appr = (
                    bool(appr_resp.json().get("result", False))
                    if appr_resp.status_code == 200
                    else False
                )
                dec = (
                    "DENY"
                    if not allowed
                    else ("REQUIRE_APPROVAL" if req_appr else "ALLOW_AUTO")
                )
                return {"decision": dec, "deny_reasons": deny_reasons, "source": "opa"}
        except Exception:
            self._opa_available = False
            self._last_failure_time = time.time()
            if self._fallback_to_local:
                dec, reasons = evaluate_local_abac(input_doc)
                return {"decision": dec, "deny_reasons": reasons, "source": "local"}
            return {
                "decision": "DENY",
                "deny_reasons": ["opa_unavailable_deny_closed"],
                "source": "error",
            }

    async def evaluate(
        self,
        action_type: str = "",
        healing_level: int = 1,
        blast_radius: str = "pod_or_instance",
        in_cooldown: bool = False,
        governance_cb_open: bool = False,
        confidence: float = 1.0,
        human_approved: bool = False,
        **kwargs: Any,
    ) -> PolicyDecision:
        action_dict = {
            "tool_name": kwargs.get("tool_name") or action_type,
            "action_type": action_type,
            "parameters": kwargs.get("parameters") or {},
            "is_destructive": kwargs.get("is_destructive", False),
            "blast_radius": blast_radius,
            "allows_unbacked_rollback": kwargs.get("allows_unbacked_rollback", False),
            "has_rollback": kwargs.get("has_rollback", True),
            "confidence": confidence,
        }
        inp = build_opa_input_for_action(
            action_dict,
            {"confidence": confidence},
            cooldown_active=in_cooldown,
            circuit_breaker_tripped=governance_cb_open,
        )
        res = await self.evaluate_action(inp)
        dec = res["decision"]
        allowed = dec in ("ALLOW_AUTO", "REQUIRE_APPROVAL")
        req_approval = (
            dec == "REQUIRE_APPROVAL"
            or (dec == "DENY" and bool(res.get("deny_reasons")) and in_cooldown)
        ) and not human_approved
        return PolicyDecision(
            allowed=allowed,
            requires_approval=req_approval,
            deny_reasons=res.get("deny_reasons", []),
            source=res.get("source", "opa"),
        )


async def evaluate_opa_policy(opa_input: dict[str, Any]) -> dict[str, Any]:
    """
    Attribute-based OPA policy evaluator for multi-step remediation plans.
    Evaluates each planned step using tool metadata (blast_radius, reversible,
    platform, namespace, resource_kind, confidence) without hardcoded tool allowlists.
    """
    import os

    engine: PolicyEngine | None = None
    try:
        from nexus.services.api.status.status_api import context

        if hasattr(context, "policy_engine") and context.policy_engine is not None:
            engine = context.policy_engine
    except Exception:
        pass

    if engine is None:
        opa_url = os.getenv("NEXUS_OPA_URL", "http://localhost:8181")
        engine = PolicyEngine(opa_url=opa_url, fallback_to_local=True)

    steps = opa_input.get("steps") or []
    confidence = float(opa_input.get("confidence", 0.0))
    namespace = opa_input.get("namespace")
    resource_kind = opa_input.get("resource_kind")

    allowed = True
    requires_approval = False
    reasons: list[str] = []
    step_decisions: list[dict[str, Any]] = []

    for step in steps:
        reversible = bool(step.get("reversible", True))
        blast_radius = str(step.get("blast_radius", "medium"))
        tool_args = dict(step.get("tool_args") or {})
        if namespace and "namespace" not in tool_args:
            tool_args["namespace"] = namespace
        if resource_kind and "kind" not in tool_args:
            tool_args["kind"] = resource_kind

        action_doc = {
            "tool_name": step.get("tool_name", ""),
            "tool_arguments": tool_args,
            "blast_radius": blast_radius,
            "reversible": reversible,
            "is_destructive": not reversible,
            "has_rollback": reversible,
            "allows_unbacked_rollback": reversible,
            "confidence": confidence,
        }
        inp = build_opa_input_for_action(
            action_doc,
            {"confidence": confidence},
            cooldown_active=bool(opa_input.get("cooldown_active", False)),
            circuit_breaker_tripped=bool(
                opa_input.get("circuit_breaker_tripped", False)
            ),
        )
        res = await engine.evaluate_action(inp)
        dec = res.get("decision", "REQUIRE_APPROVAL")
        step_decisions.append({"tool_name": step.get("tool_name", ""), **res})
        if dec == "DENY":
            allowed = False
            reasons.extend(res.get("deny_reasons") or ["OPA policy denied action"])
        elif dec == "REQUIRE_APPROVAL":
            requires_approval = True

    return {
        "allowed": allowed,
        "requires_approval": requires_approval if allowed else False,
        "reason": (
            "; ".join(reasons)
            if reasons
            else (
                "Approval required by OPA policy"
                if requires_approval
                else "Auto-approved"
            )
        ),
        "decisions": step_decisions,
    }
