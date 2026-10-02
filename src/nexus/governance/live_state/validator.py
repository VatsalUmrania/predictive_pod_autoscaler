"""Main dispatch for live Kubernetes state validation."""
from __future__ import annotations

import logging
from typing import Any

from nexus.governance.live_state.deployment_checker import (
    check_deployment_degraded,
    check_scale_condition,
)
from nexus.governance.live_state.models import (
    _ALWAYS_PROCEED,
    _ALWAYS_PROCEED_UNVERIFIABLE,
    LiveStateReport,
    proceed_always,
    proceed_unverifiable,
)
from nexus.governance.live_state.pod_checker import check_pod_failure_condition

logger = logging.getLogger(__name__)


async def check_live_state(
    action_type: str,
    namespace: str,
    resource_name: str,
    k8s_core: Any,
    k8s_apps: Any,
) -> LiveStateReport:
    """Re-query Kubernetes to confirm failure condition is still present."""
    if action_type in _ALWAYS_PROCEED:
        return proceed_always(action_type)

    if action_type in _ALWAYS_PROCEED_UNVERIFIABLE:
        return proceed_unverifiable(action_type)

    if k8s_core is None or k8s_apps is None:
        try:
            from kubernetes import client as _k8s_c
            from kubernetes import config as _k8s_cfg
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
        return await check_pod_failure_condition(action_type, namespace, resource_name, k8s_core, k8s_apps)

    if action_type in ("kubectl_rollout_undo", "rollback_deployment", "k8s_rollback_deployment"):
        return await check_deployment_degraded(action_type, namespace, resource_name, k8s_apps, k8s_core)

    if action_type in ("scale_deployment", "scale_resource", "k8s_scale_deployment"):
        return await check_scale_condition(action_type, namespace, resource_name, k8s_apps)

    logger.debug(f"[LiveStateValidator] No live check defined for action {action_type!r}")
    return LiveStateReport(
        condition_still_active=True,
        verdict="proceed",
        evidence={"reason": f"no live check defined for action {action_type!r}"},
        action_type=action_type,
    )
