"""NEXUS Rollback Registry.

Captures resource state BEFORE a healing action executes and provides
undo operations in case post-checks fail.
"""
from __future__ import annotations

import logging
from typing import Any

from nexus.governance.rollback_models import PreActionState

logger = logging.getLogger(__name__)


class RollbackRegistry:
    """Captures pre-action state and provides undo operations."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run

    async def capture(
        self,
        action_type: str,
        namespace: str,
        name: str,
        k8s_apps=None,
        k8s_core=None,
    ) -> PreActionState:
        """Capture the current state of the target resource."""
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
                state.state_data["annotations_before"] = dict(dep.metadata.annotations or {})
        except Exception as exc:
            logger.warning(
                f"[RollbackRegistry] Could not capture state for {action_type} on {namespace}/{name}: {exc}"
            )

        logger.debug(f"[RollbackRegistry] Captured state for {action_type} on {namespace}/{name}: {state.state_data}")
        return state

    async def rollback(
        self,
        pre_state: PreActionState,
        k8s_apps=None,
        k8s_core=None,
    ) -> dict[str, Any]:
        """Execute the undo operation for the given action using captured pre-state."""
        action_type = pre_state.action_type
        ns = pre_state.target_namespace
        name = pre_state.target_name
        result: dict[str, Any] = {"action_type": action_type, "status": "noop"}

        logger.info(f"[RollbackRegistry] Rolling back: {action_type} on {ns}/{name}")

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
                    result.update(status="executed", message=f"Restored {ns}/{name} → {prev_replicas} replicas")
                except Exception as exc:
                    result.update(status="failed", error=str(exc))
            else:
                result.update(status="skipped", reason="k8s_apps client not provided")

        elif action_type == "patch_annotation":
            if self.dry_run:
                result.update(status="dry_run", message=f"Would remove nexus annotations from {ns}/{name}")
                return result

            if k8s_apps:
                try:
                    dep = k8s_apps.read_namespaced_deployment(name, ns)
                    anns = dict(dep.metadata.annotations or {})
                    nexus_keys = [k for k in anns if k.startswith("nexus.io/")]
                    for k in nexus_keys:
                        anns[k] = None
                    patch = {"metadata": {"annotations": anns}}
                    k8s_apps.patch_namespaced_deployment(name, ns, patch)
                    result.update(status="executed", message=f"Removed {len(nexus_keys)} nexus.io/* annotations from {ns}/{name}")
                except Exception as exc:
                    result.update(status="failed", error=str(exc))
            else:
                result.update(status="skipped", reason="k8s_apps client not provided")

        elif action_type in ("restart_pod", "kubectl_rollout_undo", "flush_coredns_cache", "emit_alert"):
            result.update(status="noop", message=f"No rollback defined for action type: {action_type}")
        else:
            result.update(status="unknown", message=f"Unknown action type for rollback: {action_type}")

        logger.info(f"[RollbackRegistry] Rollback result: {result['status']}")
        return result
