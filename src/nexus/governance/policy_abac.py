"""
NEXUS Policy ABAC Input Builder & Local Evaluator
=================================================
Constructs OPA input with deterministic risk overrides and provides local fallback evaluation.
"""
from __future__ import annotations

import logging
from typing import Any

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
    if inp.get("cooldown_active"):
        deny_reasons.append("Resource is currently in remediation cooldown window")
    if not (inp.get("has_rollback_plan") or inp.get("allows_unbacked_rollback")):
        deny_reasons.append("Mutating action is missing an explicit rollback tool call")
    if deny_reasons:
        return "DENY", deny_reasons

    return "REQUIRE_APPROVAL", []
