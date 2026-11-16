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
    raw = raw_text.strip()
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def infer_symptom_fallback(
    sig: dict[str, Any],
    telem: dict[str, Any],
    raw_content: str,
    tname: str,
    prov: str,
    urn: str,
) -> dict[str, Any]:
    raw_str = f"{json.dumps(sig, default=str)} {json.dumps(telem, default=str)} {raw_content}".lower()
    is_oom = any(w in raw_str for w in ("oom", "exit code 137", "oomkilled", "out of memory"))
    is_crash = any(w in raw_str for w in ("crashloop", "backoff", "exit code 1", "failed container", "error"))
    is_traffic = any(w in raw_str for w in ("traffic_spike", "high_traffic", "429", "throttl"))

    if is_oom:
        return {
            "root_cause_summary": f"Pod {tname} container terminated due to OOM memory exhaustion.",
            "affected_resource_urn": urn,
            "provider": prov,
            "confidence": 0.88,
            "requires_remediation": True,
            "failure_class": "resource_exhaustion",
            "suggested_action": "k8s_patch_resource_limits",
            "action_params": {},
        }
    if is_crash:
        return {
            "root_cause_summary": f"Pod {tname} is in CrashLoopBackOff due to deployment error.",
            "affected_resource_urn": urn,
            "provider": prov,
            "confidence": 0.88,
            "requires_remediation": True,
            "failure_class": "bad_deploy",
            "suggested_action": "k8s_rollback_deployment",
            "action_params": {},
        }
    if is_traffic:
        return {
            "root_cause_summary": f"Service {tname} experiencing traffic surge.",
            "affected_resource_urn": urn,
            "provider": prov,
            "confidence": 0.85,
            "requires_remediation": True,
            "failure_class": "cascading_failure",
            "suggested_action": "k8s_scale_resource",
            "action_params": {},
        }
    return {
        "root_cause_summary": f"Investigation completed for {tname}; no root cause conclusively proven",
        "affected_resource_urn": urn,
        "provider": prov,
        "confidence": 0.0,
        "requires_remediation": False,
        "failure_class": "unknown",
        "suggested_action": None,
        "action_params": {},
    }
