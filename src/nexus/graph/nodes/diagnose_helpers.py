"""
Observation Truncation and Diagnostic Helpers for NEXUS Diagnose Nodes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from langchain_core.tools import StructuredTool

from nexus.tools.registry import default_registry

logger = logging.getLogger(__name__)


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


def get_hardened_diagnostic_tools(timeout_s: float = 5.0) -> list[StructuredTool]:
    """Retrieve diagnostic tools from registry with enforced 5s timeout and observation truncation."""
    raw_tools = default_registry.get_diagnostic_tools(as_langchain=True)
    hardened: list[StructuredTool] = []
    for tool in raw_tools:
        orig_coro = tool.coroutine
        tool_name = tool.name

        async def _wrapped(orig_fn=orig_coro, name=tool_name, **kwargs: Any) -> Any:
            try:
                raw_res = await asyncio.wait_for(orig_fn(**kwargs), timeout=timeout_s)
                return truncate_observation(raw_res)
            except asyncio.TimeoutError:
                logger.warning(
                    "[Diagnose Tool] Tool '%s' timed out after %.1fs", name, timeout_s
                )
                return {
                    "success": False,
                    "error": f"Tool '{name}' timed out after {timeout_s}s",
                }
            except Exception as exc:
                logger.warning("[Diagnose Tool] Tool '%s' failed: %s", name, exc)
                return {"success": False, "error": str(exc)}

        wrapped = StructuredTool.from_function(
            func=tool.func,
            coroutine=_wrapped,
            name=tool.name,
            description=tool.description,
            args_schema=tool.args_schema,
        )
        wrapped.metadata = getattr(tool, "metadata", {})
        hardened.append(wrapped)
    return hardened


def parse_rca_json(raw_text: str) -> dict[str, Any] | None:
    """Parse JSON object containing root cause analysis from raw LLM text."""
    raw = raw_text.strip()
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None
