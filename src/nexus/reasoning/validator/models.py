"""
Data structures and requirements for RCA validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Penalty applied when LLM and rule engine disagree on failure_class
_DISAGREEMENT_PENALTY: float = 0.15

# Penalty applied when evidence gates find missing signals
_EVIDENCE_GAP_PENALTY: float = 0.20

# Penalty applied when cascading_failure is claimed without multi-agent evidence
_CASCADING_WITHOUT_DIVERSITY_PENALTY: float = 0.15


@dataclass(frozen=True)
class EvidenceRequirement:
    must_have_any: frozenset[str] = field(default_factory=frozenset)
    must_also_have: frozenset[str] = field(default_factory=frozenset)


_EVIDENCE_REQUIREMENTS: dict[str, EvidenceRequirement] = {
    "bad_deploy": EvidenceRequirement(
        must_have_any=frozenset({"pod_crashloop", "high_error_rate", "rollout_stuck"}),
        must_also_have=frozenset({"deploy_event"}),
    ),
    "resource_exhaustion": EvidenceRequirement(
        must_have_any=frozenset(
            {
                "pod_oomkilled",
                "hpa_maxed",
                "lambda_oom",
                "lambda_throttle_spike",
                "db_connection_exhaustion",
            }
        ),
    ),
    "dependency_failure": EvidenceRequirement(
        must_have_any=frozenset(
            {
                "dns_resolution_failure",
                "upstream_down",
                "sqs_dlq_depth_high",
                "dynamo_system_error",
            }
        ),
    ),
    "config_error": EvidenceRequirement(
        must_have_any=frozenset(
            {
                "env_contract_violation",
                "secret_committed",
                "pod_crashloop",
            }
        ),
    ),
    "cascading_failure": EvidenceRequirement(
        must_have_any=frozenset(
            {
                "high_error_rate",
                "pod_crashloop",
                "deployment_degraded",
            }
        ),
    ),
    "unknown": EvidenceRequirement(),
}

_ACTION_EVIDENCE: dict[str, frozenset[str]] = {
    "restart_pod": frozenset({"pod_crashloop", "pod_oomkilled", "pod_pending"}),
    "restart_deployment": frozenset(
        {
            "pod_crashloop",
            "pod_oomkilled",
            "deployment_degraded",
            "rollout_stuck",
        }
    ),
    "k8s_restart_deployment": frozenset(
        {
            "pod_crashloop",
            "pod_oomkilled",
            "deployment_degraded",
            "rollout_stuck",
        }
    ),
    "kubectl_rollout_undo": frozenset(
        {
            "pod_crashloop",
            "high_error_rate",
            "rollout_stuck",
        }
    ),
    "rollback_deployment": frozenset(
        {
            "pod_crashloop",
            "high_error_rate",
            "rollout_stuck",
        }
    ),
    "k8s_rollback_deployment": frozenset(
        {
            "pod_crashloop",
            "high_error_rate",
            "rollout_stuck",
        }
    ),
    "scale_deployment": frozenset({"hpa_maxed", "high_error_rate", "pod_pending"}),
    "k8s_scale_deployment": frozenset({"hpa_maxed", "high_error_rate", "pod_pending"}),
    "scale_resource": frozenset(
        {
            "hpa_maxed",
            "high_error_rate",
            "pod_pending",
            "lambda_throttle_spike",
        }
    ),
    "aws_update_lambda_memory": frozenset({"lambda_oom", "lambda_timeout"}),
    "aws_update_lambda_timeout": frozenset({"lambda_timeout"}),
    "aws_replay_dlq": frozenset({"sqs_dlq_depth_high"}),
    "flush_coredns_cache": frozenset({"dns_resolution_failure"}),
    "k8s_remove_command_override": frozenset(
        {
            "pod_crashloop",
            "high_error_rate",
            "rollout_stuck",
        }
    ),
    "remove_command_override": frozenset(
        {
            "pod_crashloop",
            "high_error_rate",
            "rollout_stuck",
        }
    ),
    "emit_alert": frozenset(),
    "patch_annotation": frozenset(),
    "patch_configmap": frozenset(),
    "k8s_patch_configmap": frozenset(),
    "cordon_node": frozenset(),
    "drain_node": frozenset(),
}


def _normalize_signals(sigs: frozenset[str] | set[str]) -> set[str]:
    res = set(sigs)
    for s in sigs:
        s_clean = s.lower().replace("-", "_")
        if s_clean in ("crashloopbackoff", "crashloop", "crash_loop", "pod_crash_loop"):
            res.add("pod_crashloop")
        elif s_clean in ("oomkilled", "oom_killed", "pod_oom"):
            res.add("pod_oomkilled")
    return res


@dataclass
class ValidationVerdict:
    """Result of the RCA validation harness."""

    passed: bool
    block_reason: str | None
    confidence_delta: float
    consistency_note: str
    evidence_gaps: list[str] = field(default_factory=list)

    @property
    def has_penalty(self) -> bool:
        return self.confidence_delta < 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "block_reason": self.block_reason,
            "confidence_delta": round(self.confidence_delta, 3),
            "consistency_note": self.consistency_note,
            "evidence_gaps": self.evidence_gaps,
        }


_VERDICT_PASS = ValidationVerdict(
    passed=True,
    block_reason=None,
    confidence_delta=0.0,
    consistency_note="rule-based source — no LLM validation needed",
)
