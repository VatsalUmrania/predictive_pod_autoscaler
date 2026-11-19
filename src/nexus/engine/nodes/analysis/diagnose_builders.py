"""
NEXUS Diagnostic Message & Observation Formatting Utilities
===========================================================
Pure message formatting and observation truncation helpers (zero keyword heuristics).
"""

from __future__ import annotations

from typing import Any


def truncate_observation(
    value: Any, max_lines: int = 100, max_bytes: int = 4096
) -> Any:
    """Enforce strict truncation on tool observations: maximum 100 lines or 4KB text."""
    if isinstance(value, str):
        lines = value.splitlines()
        truncated_lines = False
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            truncated_lines = True
        text = "\n".join(lines)
        raw_bytes = text.encode("utf-8")
        if len(raw_bytes) > max_bytes:
            text = raw_bytes[:max_bytes].decode("utf-8", errors="ignore")
            return text + "\n[... truncated: output exceeded 4KB ...]"
        elif truncated_lines:
            return text + "\n[... truncated: output exceeded 100 lines ...]"
        return text
    elif isinstance(value, dict):
        return {
            k: truncate_observation(v, max_lines, max_bytes) for k, v in value.items()
        }
    elif isinstance(value, list):
        return [truncate_observation(item, max_lines, max_bytes) for item in value]
    return value


__all__ = ["truncate_observation"]
