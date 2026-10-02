"""
NEXUS Network Agent Facade
==========================
Backward-compatibility facade for NetworkAgent.
"""

from nexus.agents.network import (
    NetworkAgent,
    discover_k8s_service_hostnames,
)

__all__ = ["NetworkAgent", "discover_k8s_service_hostnames"]
