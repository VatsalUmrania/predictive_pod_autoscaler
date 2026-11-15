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

from nexus.governance.policy_models import PolicyDecision

logger = logging.getLogger(__name__)

DESTRUCTIVE_VERBS = (
    "delete",
    "terminate",
    "drop",
    "purge",
    "remove",
    "destroy",
    "deregister",
)
RESTRICTED_AWS_SERVICES = frozenset(
    {"iam", "kms", "organizations", "sts", "secretsmanager", "ssm", "billing"}
)
STATEFUL_KINDS_OR_SERVICES = frozenset(
    {
        "rds",
        "dynamodb",
        "s3",
        "elasticache",
        "StatefulSet",
        "PersistentVolumeClaim",
        "PersistentVolume",
    }
)


def build_opa_input_for_action(
    action: dict[str, Any],
    rca_report: dict[str, Any] | None = None,
    cooldown_active: bool = False,
    circuit_breaker_tripped: bool = False,
    cost_delta_pct: float = 0.0,
) -> dict[str, Any]:
    """Construct attribute-based OPA input with deterministic risk overrides."""
    tool_meta = None
    try:
        from nexus.tools.registry import default_registry

        tool_meta = default_registry.get(action.get("tool_name", ""))
    except Exception:
        pass
    args = action.get("tool_arguments") or action.get("parameters") or {}
    op = str(
        args.get("operation")
        or args.get("mutation_type")
        or action.get("action_type")
        or action.get("tool_name", "")
    ).lower()
    service_or_kind = str(args.get("service") or args.get("kind") or "")
    if not service_or_kind:
        for s in ("rds", "dynamodb", "s3", "lambda", "ecs", "ec2", "sqs"):
            if s in op:
                service_or_kind = s
                break
    is_dest_meta = (
        bool(getattr(tool_meta, "is_destructive", False)) if tool_meta else False
    )
    is_destructive_verb = (
        (any(v in op for v in DESTRUCTIVE_VERBS) and "delete_pod" not in op)
        or bool(action.get("is_destructive"))
        or is_dest_meta
    )
    is_restricted_service = service_or_kind.lower() in RESTRICTED_AWS_SERVICES
    is_stateful = (
        service_or_kind in STATEFUL_KINDS_OR_SERVICES
        or action.get("blast_radius") == "stateful_data"
    )
    raw_br = (
        action.get("blast_radius")
        or getattr(tool_meta, "blast_radius", None)
        or "pod_or_instance"
    )
    blast_radius = (
        "stateful_data"
        if is_stateful
        else ("account_wide" if raw_br == "account_wide" else raw_br)
    )

    has_rollback = bool(
        (
            action.get("rollback_tool_name")
            or action.get("rollback_tool")
            or action.get("rollback_step")
        )
        and (
            action.get("rollback_arguments")
            or action.get("rollback_parameters")
            or True
        )
    ) and bool(action.get("has_rollback", True))
    if action.get("has_rollback") is False:
        has_rollback = False

    allows_unbacked = bool(
        action.get("allows_unbacked_rollback")
        or getattr(tool_meta, "allows_unbacked_rollback", False)
    )
    conf_val = (
        (rca_report.get("confidence") if rca_report else None)
        or action.get("confidence", 0.85)
        or 0.85
    )
    rca_conf = float(conf_val)

    return {
        "tool_name": action.get("tool_name", ""),
        "service_or_kind": service_or_kind,
        "operation": op,
        "action_category": (
            "destructive"
            if is_destructive_verb
            else action.get(
                "action_category",
                (
                    "restart"
                    if "restart" in op
                    else "scale" if "scale" in op else "config_patch"
                ),
            )
        ),
        "blast_radius": blast_radius,
        "is_destructive": is_destructive_verb,
        "reversible": (
            False if is_destructive_verb else bool(action.get("reversible", True))
        ),
        "has_rollback_plan": has_rollback,
        "allows_unbacked_rollback": allows_unbacked,
        "restricted_security_service": is_restricted_service,
        "rca_confidence": rca_conf,
        "cooldown_active": cooldown_active,
        "circuit_breaker_tripped": circuit_breaker_tripped,
        "estimated_cost_delta_pct": cost_delta_pct,
    }


def evaluate_local_abac(inp: dict[str, Any]) -> tuple[str, list[str]]:
    """Local Python evaluation of nexus_policies.rego ABAC rules when OPA daemon is offline."""
    deny_reasons: list[str] = []
    if inp.get("restricted_security_service"):
        deny_reasons.append(
            "Mutations on security/identity services (IAM/KMS/STS/Secrets) are strictly prohibited"
        )
    if inp.get("blast_radius") == "stateful_data" and (
        inp.get("action_category") == "destructive" or inp.get("is_destructive")
    ):
        deny_reasons.append(
            "Destructive or irreversible mutations on stateful data stores are prohibited"
        )
    if inp.get("action_category") == "destructive" or inp.get("is_destructive"):
        deny_reasons.append("Tool is destructive — blocked by zero-trust governance")
    if inp.get("blast_radius") == "account_wide":
        deny_reasons.append(
            "Tool has account_wide blast radius — blocked by zero-trust governance"
        )
    if inp.get("circuit_breaker_tripped"):
        deny_reasons.append("Circuit breaker is tripped for target resource")
    if deny_reasons:
        return "DENY", deny_reasons

    has_rb = inp.get("has_rollback_plan") or inp.get("allows_unbacked_rollback")
    is_auto = (
        inp.get("reversible") is True
        and inp.get("action_category") in ("restart", "scale")
        and inp.get("blast_radius") == "pod_or_instance"
        and float(inp.get("rca_confidence", 0.0)) >= 0.85
        and inp.get("cooldown_active") is False
        and float(inp.get("estimated_cost_delta_pct", 0.0)) <= 25.0
        and has_rb
    )
    return ("ALLOW_AUTO" if is_auto else "REQUIRE_APPROVAL"), []


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
