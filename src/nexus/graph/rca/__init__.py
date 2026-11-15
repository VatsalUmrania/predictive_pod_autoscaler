"""NEXUS LangGraph Multi-Agent Root Cause Analysis (RCA) package."""

from typing import Any

from nexus.graph.rca.result import (
    VALID_FAILURE_CLASSES,
    EvidenceCitation,
    RCAReport,
    RCAResult,
)


class RCALeadSynthesizer:
    """Compatibility stub for legacy RCALeadSynthesizer (superseded by LangGraph ReAct agent)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass


__all__ = [
    "EvidenceCitation",
    "RCAReport",
    "RCAResult",
    "VALID_FAILURE_CLASSES",
    "RCALeadSynthesizer",
]
