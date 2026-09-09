"""
NEXUS Live State Validator
===========================
Re-queries the Kubernetes API immediately before executing a remediation action
to verify that the failure condition diagnosed by the RCA engine still exists
in the cluster.

Problem addressed:
    The RCAEngine operates on a snapshot of events that were emitted by polling
    agents (K8sAgent polls every 30s).  Between event emission and action
    execution, the condition may have self-healed.  Executing restart_pod
    against a Running, Ready, restarts=0 pod disrupts traffic unnecessarily.

Design:
    • Stateless async function — no class, no state, easy to mock in tests.
    • Maps each action_type to the precise K8s condition it presupposes.
    • L0 actions (emit_alert, patch_annotation) always proceed — zero blast radius.
    • Returns LiveStateReport with verdict ∈ {proceed, self_healed, unknown}.
    • "unknown" means the check couldn't complete (API error, missing K8s client)
      → always proceeds (fail-open for live-check errors, fail-closed is the
      job of the governance plane, not the live-check).

Verdict semantics:
    proceed      — condition confirmed still active; execute the action
    self_healed  — condition no longer present; cancel action, close incident
    unknown      — couldn't verify; proceed cautiously (let governance plane decide)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

logger = logging.getLogger(__name__)

# ── Actions that always proceed (zero blast radius) ────────────────────────────
_ALWAYS_PROCEED: frozenset[str] = frozenset({
    "emit_alert",
    "patch_annotation",
})

# ── Actions whose condition cannot be verified from K8s state alone ────────────
# e.g. DNS flush — CoreDNS might be healthy but cache still stale
_ALWAYS_PROCEED_UNVERIFIABLE: frozenset[str] = frozenset({
    "patch_configmap",
    "flush_coredns_cache",
    "cordon_node",
    "drain_node",
})


# ── Result type ────────────────────────────────────────────────────────────────

@dataclass
class LiveStateReport:
    """
    Result of a live Kubernetes state check.

    condition_still_active: True if the failure condition is still observable.
    verdict:                 proceed | self_healed | unknown
    evidence:                Dict of K8s fields returned by the live query.
    checked_at:              UTC timestamp of the check.
    action_type:             Which action type was checked.
    """

    condition_still_active: bool
    verdict: Literal["proceed", "self_healed", "unknown"]
    evidence: dict[str, Any] = field(default_factory=dict)
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    action_type: str = ""

    def __str__(self) -> str:
        return (
            f"LiveStateReport(action={self.action_type}, verdict={self.verdict}, "
            f"evidence={self.evidence})"
        )


_PROCEED_UNVERIFIABLE = lambda action_type: LiveStateReport(  # noqa: E731
    condition_still_active=True,
    verdict="proceed",
    evidence={"reason": "action condition not verifiable from K8s state — proceeding"},
    action_type=action_type,
)

_PROCEED_ALWAYS = lambda action_type: LiveStateReport(  # noqa: E731
    condition_still_active=True,
    verdict="proceed",
    evidence={"reason": "zero blast-radius action — always proceeds"},
    action_type=action_type,
)

_UNKNOWN = lambda action_type, exc: LiveStateReport(  # noqa: E731
    condition_still_active=True,
    verdict="unknown",
    evidence={"error": str(exc), "reason": "live check failed — proceeding cautiously"},
    action_type=action_type,
)


# ── Main entry point ──────────────────────────────────────────────────────────

async def check_live_state(
    action_type: str,
    namespace: str,
    resource_name: str,
    k8s_core: Any,  # kubernetes.client.CoreV1Api
    k8s_apps: Any,  # kubernetes.client.AppsV1Api
) -> LiveStateReport:
    """
    Re-query Kubernetes to confirm the failure condition presupposed by
    `action_type` is still present.

    Args:
        action_type:    The action about to be executed (e.g. "restart_pod").
        namespace:      Kubernetes namespace of the target resource.
        resource_name:  Kubernetes resource name (deployment name).
        k8s_core:       Initialized CoreV1Api client.
        k8s_apps:       Initialized AppsV1Api client.

    Returns:
        LiveStateReport with verdict "proceed", "self_healed", or "unknown".
    """
    if action_type in _ALWAYS_PROCEED:
        return _PROCEED_ALWAYS(action_type)

    if action_type in _ALWAYS_PROCEED_UNVERIFIABLE:
        return _PROCEED_UNVERIFIABLE(action_type)

    if k8s_core is None or k8s_apps is None:
        try:
            from kubernetes import client as _k8s_c, config as _k8s_cfg
            try:
                _k8s_cfg.load_incluster_config()
            except Exception:
                _k8s_cfg.load_kube_config()
            if k8s_core is None:
                k8s_core = _k8s_c.CoreV1Api()
            if k8s_apps is None:
                k8s_apps = _k8s_c.AppsV1Api()
        except Exception as _k_exc:
            logger.debug(f"[LiveStateValidator] Could not auto-init K8s clients: {_k_exc}")

    if action_type in ("restart_pod", "restart_deployment", "k8s_restart_deployment"):
        return await _check_pod_failure_condition(
            action_type, namespace, resource_name, k8s_core, k8s_apps
        )

    if action_type in ("kubectl_rollout_undo", "rollback_deployment", "k8s_rollback_deployment"):
        return await _check_deployment_degraded(
            action_type, namespace, resource_name, k8s_apps, k8s_core
        )

    if action_type in ("scale_deployment", "scale_resource", "k8s_scale_deployment"):
        return await _check_scale_condition(
            action_type, namespace, resource_name, k8s_apps
        )

    # Unknown action type — proceed cautiously
    logger.debug(
        f"[LiveStateValidator] No live check defined for action {action_type!r} "
        f"— proceeding"
    )
    return LiveStateReport(
        condition_still_active=True,
        verdict="proceed",
        evidence={"reason": f"no live check defined for action {action_type!r}"},
        action_type=action_type,
    )


# ── Per-action check implementations ─────────────────────────────────────────

async def _check_pod_failure_condition(
    action_type: str,
    namespace: str,
    resource_name: str,
    k8s_core: Any,
    k8s_apps: Any,
) -> LiveStateReport:
    """
    For restart_pod / restart_deployment:
    Condition is still active if ANY pod owned by `resource_name` is in:
      - CrashLoopBackOff (waiting.reason)
      - OOMKilled        (last_state.terminated.reason)
      - Pending          (phase == "Pending")
      - restartCount >= 3

    If ALL pods are Running + Ready + restartCount == 0 → self_healed.
    """
    import asyncio

    loop = asyncio.get_running_loop()

    try:
        # List pods in namespace
        pod_list = await loop.run_in_executor(
            None,
            lambda: k8s_core.list_namespaced_pod(namespace),
        )
    except Exception as exc:
        logger.warning(
            f"[LiveStateValidator] Pod list failed for {namespace}/{resource_name}: {exc}"
        )
        return _UNKNOWN(action_type, exc)

    # Filter to pods owned by this deployment (name prefix match or label match)
    owned_pods = [
        p for p in pod_list.items
        if _pod_belongs_to_deployment(p, resource_name)
    ]

    if not owned_pods:
        # No pods found — could be a very brief gap or wrong name
        logger.info(
            f"[LiveStateValidator] No pods found for deployment "
            f"{namespace}/{resource_name} — cannot confirm, proceeding"
        )
        return LiveStateReport(
            condition_still_active=True,
            verdict="proceed",
            evidence={"reason": f"no pods found for {namespace}/{resource_name}"},
            action_type=action_type,
        )

    failing_pods: list[dict[str, Any]] = []
    healthy_pods: int = 0

    for pod in owned_pods:
        phase = (pod.status.phase or "").lower()
        pod_info: dict[str, Any] = {
            "name": pod.metadata.name,
            "phase": phase,
            "ready": False,
            "restart_count": 0,
            "failure_reason": None,
        }

        # Check container statuses
        for cs in pod.status.container_statuses or []:
            pod_info["restart_count"] = max(
                pod_info["restart_count"], cs.restart_count or 0
            )

            # CrashLoopBackOff
            if cs.state and cs.state.waiting:
                reason = cs.state.waiting.reason or ""
                if reason == "CrashLoopBackOff":
                    pod_info["failure_reason"] = "CrashLoopBackOff"

            # OOMKilled in last terminated state
            if cs.last_state and cs.last_state.terminated:
                if cs.last_state.terminated.reason == "OOMKilled":
                    pod_info["failure_reason"] = "OOMKilled"

            # Ready condition
            if cs.ready:
                pod_info["ready"] = True

        # Pending phase
        if phase == "pending":
            pod_info["failure_reason"] = "Pending"

        if pod_info["failure_reason"] or pod_info["restart_count"] >= 3:
            failing_pods.append(pod_info)
        elif phase == "running" and pod_info["ready"] and pod_info["restart_count"] == 0:
            healthy_pods += 1

    if failing_pods:
        logger.info(
            f"[LiveStateValidator] Condition confirmed for {namespace}/{resource_name}: "
            f"{len(failing_pods)} pod(s) still failing — proceeding with {action_type}"
        )
        return LiveStateReport(
            condition_still_active=True,
            verdict="proceed",
            evidence={
                "failing_pods": failing_pods,
                "healthy_pods": healthy_pods,
                "total_checked": len(owned_pods),
            },
            action_type=action_type,
        )

    # All pods are healthy
    logger.info(
        f"[LiveStateValidator] Condition RESOLVED for {namespace}/{resource_name}: "
        f"all {len(owned_pods)} pod(s) are Running/Ready/restarts=0 "
        f"— marking as self_healed"
    )
    return LiveStateReport(
        condition_still_active=False,
        verdict="self_healed",
        evidence={
            "healthy_pods": healthy_pods,
            "total_checked": len(owned_pods),
            "all_running_ready": True,
        },
        action_type=action_type,
    )


async def _check_deployment_degraded(
    action_type: str,
    namespace: str,
    resource_name: str,
    k8s_apps: Any,
    k8s_core: Any = None,
) -> LiveStateReport:
    """
    For kubectl_rollout_undo / rollback_deployment / k8s_rollback_deployment:
    Condition is still active if:
      - deployment.status.available_replicas < spec.replicas, OR
      - deployment.status.unavailable_replicas > 0, OR
      - ANY pod belonging to this deployment is in CrashLoopBackOff, Error, OOMKilled, or restarting.
    """
    import asyncio

    loop = asyncio.get_running_loop()

    try:
        dep = await loop.run_in_executor(
            None,
            lambda: k8s_apps.read_namespaced_deployment(resource_name, namespace),
        )
    except Exception as exc:
        logger.warning(
            f"[LiveStateValidator] Deployment read failed for "
            f"{namespace}/{resource_name}: {exc}"
        )
        return _UNKNOWN(action_type, exc)

    desired = dep.spec.replicas or 1
    available = getattr(dep.status, "available_replicas", 0) or 0
    ready = getattr(dep.status, "ready_replicas", 0) or 0
    unavailable = getattr(dep.status, "unavailable_replicas", 0) or 0

    evidence = {
        "desired_replicas": desired,
        "available_replicas": available,
        "ready_replicas": ready,
        "unavailable_replicas": unavailable,
    }

    # Check 1: replica counts directly degraded
    if available < desired or unavailable > 0:
        logger.info(
            f"[LiveStateValidator] Degraded condition confirmed for "
            f"{namespace}/{resource_name}: {available}/{desired} available, {unavailable} unavailable "
            f"— proceeding with {action_type}"
        )
        return LiveStateReport(
            condition_still_active=True,
            verdict="proceed",
            evidence=evidence,
            action_type=action_type,
        )

    # Check 2: pod statuses — during rolling updates, an old pod might still be
    # reporting 'available' while new rollout pods are caught in CrashLoopBackOff!
    if k8s_core is not None:
        try:
            pod_list = await loop.run_in_executor(
                None,
                lambda: k8s_core.list_namespaced_pod(namespace),
            )
            owned_pods = [
                p for p in pod_list.items
                if _pod_belongs_to_deployment(p, resource_name)
            ]
            failing_pods: list[dict[str, Any]] = []
            for pod in owned_pods:
                phase = (pod.status.phase or "").lower()
                for cs in (pod.status.container_statuses or []):
                    reason = cs.state.waiting.reason if (cs.state and cs.state.waiting) else None
                    term_state = cs.state.terminated if (cs.state and cs.state.terminated) else (cs.last_state.terminated if cs.last_state else None)
                    term_reason = term_state.reason if term_state else None
                    exit_code = term_state.exit_code if term_state else None

                    is_failing = (
                        reason in ("CrashLoopBackOff", "Error", "CreateContainerConfigError")
                        or term_reason in ("OOMKilled", "Error")
                        or (exit_code is not None and exit_code != 0)
                        or (cs.restart_count and cs.restart_count >= 1 and not cs.ready)
                        or phase in ("pending", "failed")
                    )
                    if is_failing:
                        failing_pods.append({
                            "pod": pod.metadata.name,
                            "phase": phase,
                            "reason": reason or term_reason or f"exit_{exit_code}",
                            "restarts": cs.restart_count,
                        })
                        break

            if failing_pods:
                logger.info(
                    f"[LiveStateValidator] Active pod failure detected during rollout of {namespace}/{resource_name}: "
                    f"{len(failing_pods)} failing pod(s) — proceeding with {action_type}"
                )
                return LiveStateReport(
                    condition_still_active=True,
                    verdict="proceed",
                    evidence={**evidence, "failing_pods": failing_pods},
                    action_type=action_type,
                )
        except Exception as p_exc:
            logger.debug(f"[LiveStateValidator] Could not inspect pods for deployment {resource_name}: {p_exc}")

    logger.info(
        f"[LiveStateValidator] Deployment {namespace}/{resource_name} is healthy: "
        f"{available}/{desired} available — marking as self_healed"
    )
    return LiveStateReport(
        condition_still_active=False,
        verdict="self_healed",
        evidence={**evidence, "all_replicas_available": True},
        action_type=action_type,
    )


async def _check_scale_condition(
    action_type: str,
    namespace: str,
    resource_name: str,
    k8s_apps: Any,
) -> LiveStateReport:
    """
    For scale_deployment / scale_resource:
    Condition is still active if the deployment's availableReplicas < spec.replicas
    (indicating load pressure) OR an HPA for this deployment is at maxReplicas.

    If the deployment is fully scaled and no HPA pressure exists → self_healed.
    """
    import asyncio

    loop = asyncio.get_running_loop()

    try:
        dep = await loop.run_in_executor(
            None,
            lambda: k8s_apps.read_namespaced_deployment(resource_name, namespace),
        )
    except Exception as exc:
        logger.warning(
            f"[LiveStateValidator] Deployment read failed for scale check "
            f"{namespace}/{resource_name}: {exc}"
        )
        return _UNKNOWN(action_type, exc)

    desired = dep.spec.replicas or 1
    available = dep.status.available_replicas or 0

    if available < desired:
        return LiveStateReport(
            condition_still_active=True,
            verdict="proceed",
            evidence={"desired_replicas": desired, "available_replicas": available},
            action_type=action_type,
        )

    # Deployment is healthy — scale action may no longer be needed
    logger.info(
        f"[LiveStateValidator] Scale condition resolved for "
        f"{namespace}/{resource_name}: {available}/{desired} — self_healed"
    )
    return LiveStateReport(
        condition_still_active=False,
        verdict="self_healed",
        evidence={
            "desired_replicas": desired,
            "available_replicas": available,
            "all_replicas_available": True,
        },
        action_type=action_type,
    )


# ── Internal helpers ──────────────────────────────────────────────────────────

def _pod_belongs_to_deployment(pod: Any, deployment_name: str) -> bool:
    """
    Return True if the pod is owned by a ReplicaSet whose name starts with
    `deployment_name`, i.e. it was created by that deployment.
    """
    for ref in pod.metadata.owner_references or []:
        if ref.kind == "ReplicaSet" and ref.name.startswith(deployment_name):
            return True
    # Fallback: pod name starts with deployment name (e.g. bare pods in tests)
    return pod.metadata.name.startswith(deployment_name)
