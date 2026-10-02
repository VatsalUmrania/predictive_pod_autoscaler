"""
NEXUS Multi-Agent RCA Facade.
=============================
Backward-compatible re-exports from specialized analyst submodules.
"""

from nexus.graph.rca.log_analyst import LogAnalystAgent
from nexus.graph.rca.metrics_analyst import MetricsAnalystAgent
from nexus.graph.rca.synthesizer import RCALeadSynthesizer
from nexus.graph.rca.topology_analyst import TopologyAnalystAgent

__all__ = [
    "LogAnalystAgent",
    "MetricsAnalystAgent",
    "RCALeadSynthesizer",
    "TopologyAnalystAgent",
]
