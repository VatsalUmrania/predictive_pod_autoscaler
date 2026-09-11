"""Backwards-compatibility shim for NexusOrchestrator."""
from nexus.graph.workflow import IncidentWorkflow as NexusOrchestrator

__all__ = ["NexusOrchestrator"]
