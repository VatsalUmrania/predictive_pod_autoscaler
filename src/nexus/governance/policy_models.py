"""Policy decision models and input-shaping constants for OPA."""
from __future__ import annotations

from dataclasses import dataclass, field


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


LLM_TOOL_LEVEL: dict[str, tuple[int, str]] = {
    "restart_deployment": (1, "single_deployment"),
    "scale_resource": (2, "single_deployment"),
    "rollback_deployment": (3, "single_deployment"),
    "patch_configmap": (3, "single_deployment"),
    "cordon_node": (3, "cluster_wide"),
    "drain_node": (3, "cluster_wide"),
}
