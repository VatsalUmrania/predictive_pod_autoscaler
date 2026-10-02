"""
NEXUS NGINX Agent Facade
========================
Backward-compatibility facade for NginxAgent.
"""

from nexus.agents.nginx import NginxAgent, parse_stub_status

__all__ = ["NginxAgent", "parse_stub_status"]
