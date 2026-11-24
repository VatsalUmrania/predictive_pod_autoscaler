"""NEXUS Rollback Registry.

Captures resource state and pre-registered inverse mutating tool calls
so executions can be deterministically reverted in reverse order.
"""

from __future__ import annotations

import logging
from typing import Any

from nexus.governance.rollback.rollback_models import PreActionState

logger = logging.getLogger(__name__)


class RollbackRegistry:
    """Stores pre-action states and pre-registered inverse tool actions."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self._entries: dict[str, list[dict[str, Any]]] = {}

    def register(
        self,
        incident_id: str,
        step_number: int,
        tool_name: str,
        tool_arguments: dict[str, Any],
    ) -> None:
        """Pre-register inverse rollback call BEFORE mutating the target resource."""
        if incident_id not in self._entries:
            self._entries[incident_id] = []
        self._entries[incident_id].append(
            {
                "step_number": step_number,
                "tool_name": tool_name,
                "tool_arguments": tool_arguments,
            }
        )
        logger.info(
            "[RollbackRegistry] Registered inverse call for inc=%s step=%d: %s",
            incident_id,
            step_number,
            tool_name,
        )

    async def execute_all(
        self, incident_id: str, registry: Any = None
    ) -> list[dict[str, Any]]:
        """Execute pre-registered inverse actions in reverse step order."""
        from nexus.engine.tools.registry import default_registry

        reg = registry or default_registry
        actions = self._entries.pop(incident_id, [])
        actions.sort(key=lambda a: a.get("step_number", 0), reverse=True)
        results: list[dict[str, Any]] = []

        for act in actions:
            t_name = act["tool_name"]
            t_args = act["tool_arguments"]
            logger.warning(
                "[RollbackRegistry] Executing rollback tool '%s' with args: %s",
                t_name,
                t_args,
            )
            try:
                res = await reg.execute_mutating(t_name, t_args)
                results.append(
                    {
                        "step_number": act.get("step_number"),
                        "tool_name": t_name,
                        "success": True,
                        "output": res,
                    }
                )
            except Exception as exc:
                logger.error(
                    "[RollbackRegistry] Rollback tool '%s' failed: %s", t_name, exc
                )
                results.append(
                    {
                        "step_number": act.get("step_number"),
                        "tool_name": t_name,
                        "success": False,
                        "error": str(exc),
                    }
                )
        return results

    async def capture(
        self,
        action_type: str,
        namespace: str,
        name: str,
        k8s_apps=None,
        k8s_core=None,
    ) -> PreActionState:
        """Capture the current state of the target resource (legacy helper)."""
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
                f"[RollbackRegistry] Could not capture state for {action_type} on {namespace}/{name}: {exc}"
            )
        return state

    async def rollback(
        self,
        pre_state: PreActionState,
        k8s_apps=None,
        k8s_core=None,
    ) -> dict[str, Any]:
        """Execute legacy undo operation using captured pre-state."""
        action_type = pre_state.action_type
        ns = pre_state.target_namespace
        name = pre_state.target_name
        result: dict[str, Any] = {"action_type": action_type, "status": "noop"}

        if action_type == "scale_deployment":
            prev_replicas = pre_state.state_data.get("previous_replicas")
            if prev_replicas is None:
                return {
                    "action_type": action_type,
                    "status": "skipped",
                    "reason": "no_replicas",
                }
            if self.dry_run:
                return {
                    "action_type": action_type,
                    "status": "dry_run",
                    "message": f"Scale {ns}/{name} -> {prev_replicas}",
                }
            if k8s_apps:
                try:
                    patch = {"spec": {"replicas": prev_replicas}}
                    k8s_apps.patch_namespaced_deployment_scale(name, ns, patch)
                    return {
                        "action_type": action_type,
                        "status": "executed",
                        "message": f"Restored replicas -> {prev_replicas}",
                    }
                except Exception as exc:
                    return {
                        "action_type": action_type,
                        "status": "failed",
                        "error": str(exc),
                    }
        return result
