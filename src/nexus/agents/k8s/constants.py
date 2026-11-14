"""
Kubernetes Agent Constants & Pod Parsing Helpers
"""

from __future__ import annotations

from typing import Any

SYSTEM_NAMESPACES = frozenset(
    {
        "kube-system",
        "kube-public",
        "kube-node-lease",
        "monitoring",
        "nexus",
        "ingress-nginx",
    }
)


def parse_memory_mi(mem_str: str) -> float:
    """Parse K8s memory string (e.g. '512Mi', '1Gi') to MiB."""
    multipliers = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "Ti": 1024 * 1024}
    for suffix, mult in multipliers.items():
        if mem_str.endswith(suffix):
            return float(mem_str[: -len(suffix)]) * mult
    return float(mem_str) / (1024 * 1024)


def get_container_memory_limit(pod: Any, container_name: str) -> float | None:
    """Extract memory limit for a specific container from the pod spec."""
    try:
        for cspec in pod.spec.containers:
            if (
                cspec.name == container_name
                and cspec.resources
                and cspec.resources.limits
            ):
                lim = cspec.resources.limits.get("memory")
                if lim:
                    return parse_memory_mi(lim)
    except Exception:
        pass
    return None


def get_deployment_name(pod: Any) -> str | None:
    """Extract the owning ReplicaSet -> Deployment name from pod owner refs."""
    for ref in pod.metadata.owner_references or []:
        if ref.kind == "ReplicaSet":
            parts = ref.name.rsplit("-", 1)
            return str(parts[0]) if len(parts) == 2 else str(ref.name)
    return None
