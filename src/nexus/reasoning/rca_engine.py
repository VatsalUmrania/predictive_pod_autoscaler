"""Compatibility shim: RCAResult has moved to nexus.graph.rca."""

from nexus.graph.rca import RCAResult, deterministic_baseline_rca

_rule_based_rca = deterministic_baseline_rca

__all__ = ["RCAResult", "_rule_based_rca"]
