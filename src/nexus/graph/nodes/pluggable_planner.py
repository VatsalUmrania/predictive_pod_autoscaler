"""
Planner for pluggable/future platform adapters.
"""

from __future__ import annotations

import logging
from typing import Any

from nexus.graph.state import PlanStep

logger = logging.getLogger(__name__)


def build_pluggable_platform_steps(
    platform: str,
    target_name: str,
    target_ns: str,
    target: dict[str, Any],
) -> tuple[list[PlanStep], str | None]:
    """Dynamically introspect tools from adapter for future/pluggable platforms."""
    from nexus.graph.platform import get_platform_registry

    registry = get_platform_registry()
    adapter = registry.get(platform)
    available_tools = adapter.get_tools() if adapter else []

    chosen_tool = None
    for t in available_tools:
        name_lower = getattr(t, "name", "").lower()
        if "get" not in name_lower and "describe" not in name_lower and "list" not in name_lower:
            chosen_tool = t
            break
    if not chosen_tool and available_tools:
        chosen_tool = available_tools[0]

    if chosen_tool:
        schema_props = (
            chosen_tool.args_schema.model_json_schema().get("properties", {})
            if hasattr(chosen_tool, "args_schema")
            else {}
        )
        params = {}
        for p in schema_props:
            p_lower = p.lower()
            if "target" in p_lower or "name" in p_lower or "instance" in p_lower:
                params[p] = target_name
            elif "project" in p_lower:
                uri = target.get("arn_or_uri", "")
                params[p] = uri.split("/")[1] if "/" in uri else "default-project"
            elif "zone" in p_lower or "region" in p_lower or "namespace" in p_lower:
                params[p] = target_ns
            else:
                params[p] = "default"

        return [
            PlanStep(
                step_index=0,
                tool_name=chosen_tool.name,
                parameters=params,
                description=f"Remediate {target_name} using {chosen_tool.name}",
            )
        ], None

    return [
        PlanStep(
            step_index=0,
            tool_name="generic_remediate",
            parameters={"target": target_name},
            description=f"Execute default remediation on platform {platform}",
        )
    ], None
