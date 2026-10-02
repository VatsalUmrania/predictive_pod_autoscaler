"""
Kubernetes target detection and handling predicates.
"""

from __future__ import annotations

from typing import Any

from nexus.graph.platform.base import TargetResource


def can_handle_k8s(event_or_target: dict[str, Any] | TargetResource) -> bool:
    """Check if the event or target belongs to Kubernetes."""
    if isinstance(event_or_target, TargetResource):
        return event_or_target.platform.lower() in ("kubernetes", "k8s")

    if isinstance(event_or_target, dict):
        plat = str(event_or_target.get("platform", "")).lower()
        if plat in ("kubernetes", "k8s"):
            return True
        agent = str(event_or_target.get("agent", "")).lower()
        if agent in ("k8s", "metrics", "nginx", "git", "config", "db", "network"):
            return True
        res_name = str(event_or_target.get("resource_name", ""))
        if res_name.startswith("arn:aws:"):
            return False
        if "aws" in str(event_or_target.get("namespace", "")).lower():
            return False
        if agent not in ("lambda", "apigw", "sqs", "dynamodb", "cloudwatch"):
            return True

    return False


def detect_k8s_target(events: list[dict[str, Any]]) -> TargetResource:
    """Detect the Kubernetes target resource from trigger events."""
    if not events:
        return TargetResource(platform="kubernetes", namespace="default", name="unknown", kind="deployment")

    primary_evt = events[0]
    for evt in events:
        if str(evt.get("severity", "")).lower() == "critical":
            primary_evt = evt
            break

    ctx = primary_evt.get("context", {}) if isinstance(primary_evt.get("context"), dict) else {}
    namespace = str(primary_evt.get("namespace", "default"))
    raw_kind = str(primary_evt.get("resource_kind") or "").lower()
    deployment_name = ctx.get("deployment_name") or primary_evt.get("deployment")
    pod_name = ctx.get("pod_name")

    raw_name = str(primary_evt.get("resource_name", "unknown"))
    if "/" in raw_name and not raw_name.startswith("arn:"):
        parts = raw_name.split("/", 1)
        namespace = parts[0]
        raw_name = parts[1]

    if raw_kind in ("deployment", "deploy"):
        kind = "deployment"
        name = deployment_name or raw_name
    elif raw_kind == "pod":
        kind = "pod"
        name = pod_name or raw_name
    elif deployment_name and raw_name == deployment_name:
        kind = "deployment"
        name = deployment_name
    elif pod_name and raw_name == pod_name:
        kind = "pod"
        name = pod_name
    elif "pod" in str(primary_evt.get("signal_type", "")).lower() or (
        raw_name and len(raw_name.split("-")) >= 3 and not raw_kind
    ):
        kind = "pod"
        name = pod_name or raw_name
    elif raw_name and raw_name != "unknown":
        kind = "deployment"
        name = raw_name
    elif pod_name:
        kind = "pod"
        name = pod_name
    else:
        kind = "deployment"
        name = raw_name

    labels: dict[str, str] = {}
    if pod_name:
        labels["pod_name"] = str(pod_name)
    if deployment_name:
        labels["deployment_name"] = str(deployment_name)

    return TargetResource(
        platform="kubernetes",
        namespace=namespace or "default",
        name=name,
        kind=kind,
        labels=labels,
    )
