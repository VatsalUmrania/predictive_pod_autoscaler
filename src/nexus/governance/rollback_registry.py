"""
NEXUS Rollback Registry
========================
Captures resource state BEFORE a healing action executes and provides
undo operations in case post-checks fail.

The registry ensures every healing action is reversible:

    Action                 Rollback
    ─────────────────────  ──────────────────────────────────────────────────
    restart_pod            noop    (pod restart is already idempotent)
    kubectl_rollout_undo   noop    (rollback IS the undo — re-deploy is L3)
    scale_deployment       scale_to_previous_replica_count
    flush_coredns_cache    noop    (CoreDNS self-heals from config)
    patch_annotation       remove_nexus_annotations
    emit_alert             noop    (cannot un-send an alert)

Usage in RunbookExecutor:
    pre_state = await registry.capture(action, namespace, name, k8s_apps, k8s_core)
    # ... execute action ...
    if post_check_failed:
        rb_result = await registry.rollback(pre_state, k8s_apps, k8s_core)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Pre-action state snapshot
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class PreActionState:
    """Snapshot of resource state captured before a healing action executes."""
    action_type:      str
    target_namespace: str
    target_name:      str
    captured_at:      str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    state_data:       Dict[str, Any] = field(default_factory=dict)

    @property
    def rollback_key(self) -> str:
        return f"{self.action_type}::{self.target_namespace}/{self.target_name}"


# ──────────────────────────────────────────────────────────────────────────────
# Rollback Registry
# ──────────────────────────────────────────────────────────────────────────────

class RollbackRegistry:
    """
    Captures pre-action state and provides undo operations.

    All K8s API calls are made via the clients passed in — the registry
    holds no long-lived client references so it can be shared across tests.

    Args:
        dry_run: If True, log rollback operations but do not execute them.
    """

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run

    # ── State capture ─────────────────────────────────────────────────────────

    async def capture(
        self,
        action_type: str,
        namespace: str,
        name: str,
        k8s_apps=None,
        k8s_core=None,
    ) -> PreActionState:
        """
        Capture the current state of the target resource.
        Returns a PreActionState object that can be passed to rollback().
        """
        state = PreActionState(
            action_type=action_type,
            target_namespace=namespace,
            target_name=name,
        )

        try:
            if action_type == "scale_deployment" and k8s_apps:
                dep = k8s_apps.read_namespaced_deployment(name, namespace)
                state.state_data["previous_replicas"] = dep.spec.replicas or 1

            elif action_type == "patch_annotation" and k8s_apps:
                dep = k8s_apps.read_namespaced_deployment(name, namespace)
                state.state_data["annotations_before"] = dict(
                    dep.metadata.annotations or {}
                )

        except Exception as exc:
            logger.warning(
                f"[RollbackRegistry] Could not capture state for "
                f"{action_type} on {namespace}/{name}: {exc}"
            )

        logger.debug(
            f"[RollbackRegistry] Captured state for {action_type} "
            f"on {namespace}/{name}: {state.state_data}"
        )
        return state

    # ── Rollback execution ────────────────────────────────────────────────────

    async def rollback(
        self,
        pre_state: PreActionState,
        k8s_apps=None,
        k8s_core=None,
    ) -> Dict[str, Any]:
        """
        Execute the undo operation for the given action using captured pre-state.
        Returns a result dict with status and message.
        """
        action_type = pre_state.action_type
        ns          = pre_state.target_namespace
        name        = pre_state.target_name
        result: Dict[str, Any] = {"action_type": action_type, "status": "noop"}

        logger.info(f"[RollbackRegistry] Rolling back: {action_type} on {ns}/{name}")

        # ── scale_deployment: restore previous replica count ──────────────────
        if action_type == "scale_deployment":
            prev_replicas = pre_state.state_data.get("previous_replicas")
            if prev_replicas is None:
                result.update(status="skipped", reason="previous_replicas not captured")
                return result

            if self.dry_run:
                result.update(status="dry_run", message=f"Would scale {ns}/{name} → {prev_replicas}")
                return result

            if k8s_apps:
                try:
                    patch = {"spec": {"replicas": prev_replicas}}
                    k8s_apps.patch_namespaced_deployment_scale(name, ns, patch)
                    result.update(
                        status="executed",
                        message=f"Restored {ns}/{name} → {prev_replicas} replicas",
                    )
                except Exception as exc:
                    result.update(status="failed", error=str(exc))
            else:
                result.update(status="skipped", reason="k8s_apps client not provided")

        # ── patch_annotation: remove nexus.io/* annotations ──────────────────
        elif action_type == "patch_annotation":
            if self.dry_run:
                result.update(status="dry_run", message=f"Would remove nexus annotations from {ns}/{name}")
                return result

            if k8s_apps:
                try:
                    # Get current annotations, remove nexus.io/* keys
                    dep  = k8s_apps.read_namespaced_deployment(name, ns)
                    anns = dict(dep.metadata.annotations or {})
                    nexus_keys = [k for k in anns if k.startswith("nexus.io/")]
                    for k in nexus_keys:
                        anns[k] = None   # Setting to None removes in strategic merge patch
                    patch = {"metadata": {"annotations": anns}}
                    k8s_apps.patch_namespaced_deployment(name, ns, patch)
                    result.update(
                        status="executed",
                        message=f"Removed {len(nexus_keys)} nexus.io/* annotations from {ns}/{name}",
                    )
                except Exception as exc:
                    result.update(status="failed", error=str(exc))
            else:
                result.update(status="skipped", reason="k8s_apps client not provided")

        # ── No-op actions ─────────────────────────────────────────────────────
        elif action_type in (
            "restart_pod",
            "kubectl_rollout_undo",
            "flush_coredns_cache",
            "emit_alert",
        ):
            result.update(
                status="noop",
                message=f"No rollback defined for action type: {action_type}",
            )

        else:
            result.update(
                status="unknown",
                message=f"Unknown action type for rollback: {action_type}",
            )

        logger.info(f"[RollbackRegistry] Rollback result: {result['status']} — {result.get('message', result.get('error', ''))}")
        return result
