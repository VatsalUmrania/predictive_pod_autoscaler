"""NEXUS LangGraph Root Cause Analysis (RCA) package."""

from typing import Any

from nexus.graph.rca.result import (
    DiscoveredTarget,
    RCAResult,
    VerificationResult,
)


class RCALeadSynthesizer:
    """Compatibility stub for legacy RCALeadSynthesizer (superseded by LangGraph ReAct agent)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass


__all__ = [
    "DiscoveredTarget",
    "RCAResult",
    "VerificationResult",
    "RCALeadSynthesizer",
]
