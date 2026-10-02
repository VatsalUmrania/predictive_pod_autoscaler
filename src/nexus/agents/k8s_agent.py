"""
NEXUS Kubernetes Agent Facade
=============================
Backward-compatibility facade for K8sAgent and constants.
"""

from nexus.agents.k8s import SYSTEM_NAMESPACES, K8sAgent

__all__ = ["K8sAgent", "SYSTEM_NAMESPACES"]
