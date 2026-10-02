"""
NEXUS Network Agent Package
"""

from nexus.agents.network.agent import NetworkAgent
from nexus.agents.network.discovery import discover_k8s_service_hostnames

__all__ = ["NetworkAgent", "discover_k8s_service_hostnames"]
