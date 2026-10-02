"""Models, constants, and fallback report factories for live state validation."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

_ALWAYS_PROCEED: frozenset[str] = frozenset({
    "emit_alert",
    "patch_annotation",
})

_ALWAYS_PROCEED_UNVERIFIABLE: frozenset[str] = frozenset({
    "patch_configmap",
    "flush_coredns_cache",
    "cordon_node",
    "drain_node",
})


@dataclass
class LiveStateReport:
    """Result of a live Kubernetes state check."""

    condition_still_active: bool
    verdict: Literal["proceed", "self_healed", "unknown"]
    evidence: dict[str, Any] = field(default_factory=dict)
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    action_type: str = ""

    def __str__(self) -> str:
        return f"LiveStateReport(action={self.action_type}, verdict={self.verdict}, evidence={self.evidence})"


def proceed_unverifiable(action_type: str) -> LiveStateReport:
    return LiveStateReport(
        condition_still_active=True,
        verdict="proceed",
        evidence={"reason": "action condition not verifiable from K8s state — proceeding"},
        action_type=action_type,
    )


def proceed_always(action_type: str) -> LiveStateReport:
    return LiveStateReport(
        condition_still_active=True,
        verdict="proceed",
        evidence={"reason": "zero blast-radius action — always proceeds"},
        action_type=action_type,
    )


def unknown_report(action_type: str, exc: Exception) -> LiveStateReport:
    return LiveStateReport(
        condition_still_active=True,
        verdict="unknown",
        evidence={"error": str(exc), "reason": "live check failed — proceeding cautiously"},
        action_type=action_type,
    )


def pod_belongs_to_deployment(pod: Any, deployment_name: str) -> bool:
    for ref in getattr(getattr(pod, "metadata", None), "owner_references", None) or []:
        if getattr(ref, "kind", None) == "ReplicaSet" and str(getattr(ref, "name", "")).startswith(deployment_name):
            return True
    pod_name = str(getattr(getattr(pod, "metadata", None), "name", ""))
    return bool(pod_name.startswith(deployment_name))
