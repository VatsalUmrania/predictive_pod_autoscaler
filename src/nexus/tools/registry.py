"""
NEXUS Unified Tool Registry
===========================
Central catalog managing discovery, schema reflection, read/write separation,
metadata lookup, and LangChain tool invocation for Kubernetes, AWS, and MCP tools.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from nexus.tools.base import NexusTool, NexusToolResult, ToolDomain, ToolRiskLevel
from nexus.tools.registry_helpers import (
    build_tool_schemas,
    execute_tool_with_db,
    register_legacy_tools,
    register_universal_tools,
)

logger = logging.getLogger(__name__)


@dataclass
class ToolMetadata:
    """Attribute-based metadata exposed to the planner and OPA governance gate."""

    blast_radius: str = "medium"
    reversible: bool = True
    is_destructive: bool = False
    platform: str = "k8s"
    mode: str = "mutate"


def _normalize_platform(platform: str | None) -> str | None:
    if not platform:
        return None
    p = platform.strip().lower()
    if p in ("k8s", "kubernetes", "kube"):
        return "k8s"
    return p


def _wrap_universal_callable(
    name: str,
    fn: Callable[..., Any],
    schema: type[BaseModel],
) -> StructuredTool:
    """Wrap a Pydantic-model-taking universal callable as a kwargs-accepting StructuredTool."""
    if inspect.iscoroutinefunction(fn):

        async def _acall(**kwargs: Any) -> Any:
            validated = schema.model_validate(kwargs)
            return await fn(validated)

        def _scall(**kwargs: Any) -> Any:
            validated = schema.model_validate(kwargs)
            return asyncio.run(fn(validated))

        return StructuredTool.from_function(
            func=_scall,
            coroutine=_acall,
            name=name,
            description=getattr(schema, "__doc__", "") or f"Execute {name}",
            args_schema=schema,
        )

    def _call(**kwargs: Any) -> Any:
        validated = schema.model_validate(kwargs)
        return fn(validated)

    async def _async_call(**kwargs: Any) -> Any:
        validated = schema.model_validate(kwargs)
        return await asyncio.to_thread(fn, validated)

    return StructuredTool.from_function(
        func=_call,
        coroutine=_async_call,
        name=name,
        description=getattr(schema, "__doc__", "") or f"Execute {name}",
        args_schema=schema,
    )


class ToolRegistry:
    """Registry managing read-only and mutating tools with explicit separation."""

    def __init__(self) -> None:
        self._tools: dict[str, NexusTool] = {}
        self._universal_callables: dict[
            str, tuple[Callable[..., Any], type[BaseModel], bool, str]
        ] = {}
        self._langchain_tools: dict[str, tuple[Any, ToolMetadata]] = {}
        register_universal_tools(self)
        register_legacy_tools(self)

    def register_function(
        self,
        name: str,
        fn: Callable[..., Any],
        args_schema: type[BaseModel],
        is_mutating: bool,
        provider: str,
    ) -> None:
        self._universal_callables[name] = (fn, args_schema, is_mutating, provider)

    def register_langchain_tool(
        self,
        lc_tool: Any,
        *,
        mode: str = "observe",
        platform: str = "k8s",
        metadata: ToolMetadata | None = None,
    ) -> None:
        meta = metadata or ToolMetadata(
            blast_radius="none" if mode == "observe" else "medium",
            reversible=True,
            is_destructive=False,
            platform=_normalize_platform(platform) or "k8s",
            mode=mode,
        )
        self._langchain_tools[lc_tool.name] = (lc_tool, meta)

    def register(self, tool: NexusTool) -> None:
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)
        self._universal_callables.pop(name, None)
        self._langchain_tools.pop(name, None)

    def get(self, name: str) -> NexusTool | None:
        return self._tools.get(name)

    def get_tool(self, tool_name: str) -> Any | None:
        """Return a LangChain-invocable tool (supporting .ainvoke(tool_args)) by name."""
        if tool_name in self._langchain_tools:
            return self._langchain_tools[tool_name][0]
        if tool_name in self._tools:
            return self._tools[tool_name].as_langchain_tool()
        if tool_name in self._universal_callables:
            fn, schema, _, _ = self._universal_callables[tool_name]
            return _wrap_universal_callable(tool_name, fn, schema)
        return None

    def get_metadata(self, tool_name: str) -> ToolMetadata:
        """Return attribute-based metadata for OPA governance and planning."""
        if tool_name in self._langchain_tools:
            return self._langchain_tools[tool_name][1]
        if tool_name in self._tools:
            t = self._tools[tool_name]
            is_obs = getattr(t, "risk_level", None) == ToolRiskLevel.L0_OBSERVE
            is_dest = bool(getattr(t, "is_destructive", False))
            return ToolMetadata(
                blast_radius=str(getattr(t, "blast_radius", "medium")),
                reversible=not is_dest,
                is_destructive=is_dest,
                platform=_normalize_platform(getattr(t.domain, "value", "k8s"))
                or "k8s",
                mode="observe" if is_obs else "mutate",
            )
        if tool_name in self._universal_callables:
            _, _, is_mut, provider = self._universal_callables[tool_name]
            return ToolMetadata(
                blast_radius="medium" if is_mut else "none",
                reversible=True,
                is_destructive=False,
                platform=_normalize_platform(provider) or "k8s",
                mode="mutate" if is_mut else "observe",
            )
        return ToolMetadata()

    def get_tools(
        self, mode: str = "observe", platform: str | None = None
    ) -> list[Any]:
        """Return LangChain tools filtered by mode ('observe' | 'mutate') and platform."""
        norm_plat = _normalize_platform(platform)
        want_mutating = mode.strip().lower() == "mutate"
        tools: list[Any] = []
        seen: set[str] = set()

        # 1. Native LangChain @tool functions (e.g., locate_pod_and_owner, fetch_pod_container_logs)
        for name, (lc_tool, meta) in self._langchain_tools.items():
            if (meta.mode == "mutate") != want_mutating:
                continue
            if norm_plat and _normalize_platform(meta.platform) != norm_plat:
                continue
            tools.append(lc_tool)
            seen.add(name)

        # 2. Universal callables
        for name, (fn, schema, is_mut, provider) in self._universal_callables.items():
            if name in seen or is_mut != want_mutating:
                continue
            if norm_plat and _normalize_platform(provider) != norm_plat:
                continue
            tools.append(_wrap_universal_callable(name, fn, schema))
            seen.add(name)

        # 3. Domain NexusTool instances
        for name, t in self._tools.items():
            if name in seen:
                continue
            is_mut = getattr(t, "risk_level", None) != ToolRiskLevel.L0_OBSERVE
            if is_mut != want_mutating:
                continue
            t_plat = _normalize_platform(
                getattr(getattr(t, "domain", None), "value", None)
            )
            if norm_plat and t_plat != norm_plat:
                continue
            tools.append(t.as_langchain_tool())
            seen.add(name)

        return tools

    def list_tools(
        self, domain: ToolDomain | None = None, max_risk: ToolRiskLevel | None = None
    ) -> list[NexusTool]:
        res = list(self._tools.values())
        if domain:
            res = [t for t in res if getattr(t, "domain", None) == domain]
        if max_risk:
            ro = {
                ToolRiskLevel.L0_OBSERVE: 0,
                ToolRiskLevel.L1_SAFE: 1,
                ToolRiskLevel.L2_MUTATE_APPROVAL: 2,
                ToolRiskLevel.L3_DESTRUCTIVE: 3,
            }
            res = [t for t in res if ro.get(t.risk_level, 0) <= ro.get(max_risk, 3)]
        return res

    def get_read_only_tools_as_langchain(self) -> list[Any]:
        return self.get_tools(mode="observe", platform=None)

    def get_mutating_tool_schemas(self) -> list[dict[str, Any]]:
        return build_tool_schemas(
            self._universal_callables, self._tools, is_mutating=True
        )

    def get_read_only_tool_schemas(self) -> list[dict[str, Any]]:
        return build_tool_schemas(
            self._universal_callables, self._tools, is_mutating=False
        )

    async def execute_read_only(self, tool_name: str, tool_args: dict[str, Any]) -> Any:
        if tool_name in self._langchain_tools:
            lc_tool, meta = self._langchain_tools[tool_name]
            if meta.mode != "observe":
                raise PermissionError(
                    f"Tool '{tool_name}' is mutating and cannot be called during read-only inspection."
                )
            return await lc_tool.ainvoke(tool_args)
        if tool_name in self._universal_callables:
            fn, schema, is_mutating, _ = self._universal_callables[tool_name]
            if is_mutating:
                raise PermissionError(
                    f"Tool '{tool_name}' is mutating and cannot be called during read-only inspection."
                )
            validated = schema.model_validate(tool_args)
            res = fn(validated)
            return await res if inspect.isawaitable(res) else res
        t = self.get(tool_name)
        if t and getattr(t, "risk_level", None) == ToolRiskLevel.L0_OBSERVE:
            res = await t.execute(**tool_args)
            return res.data if res.success else {"error": res.error}
        raise KeyError(f"Read-only tool '{tool_name}' not found.")

    async def execute_mutating(self, tool_name: str, tool_args: dict[str, Any]) -> Any:
        if tool_name in self._universal_callables:
            fn, schema, is_mutating, _ = self._universal_callables[tool_name]
            if not is_mutating:
                raise ValueError(f"Tool '{tool_name}' is not registered as mutating.")
            validated = schema.model_validate(tool_args)
            res = fn(validated)
            return await res if inspect.isawaitable(res) else res
        t = self.get(tool_name)
        if t:
            res = await t.execute(**tool_args)
            return res.data if res.success else {"error": res.error}
        raise KeyError(f"Mutating tool '{tool_name}' not found.")

    def load_mcp_tools(self, mcp_servers_config: list[dict[str, Any]]) -> int:
        for srv in mcp_servers_config:
            logger.info(
                "[ToolRegistry] Registered MCP Server endpoint: %s", srv.get("name")
            )
        return len(mcp_servers_config)

    def get_function_declarations(
        self, domain: ToolDomain | None = None, max_risk: ToolRiskLevel | None = None
    ) -> list[dict[str, Any]]:
        return [
            t.to_function_declaration()
            for t in self.list_tools(domain=domain, max_risk=max_risk)
            if hasattr(t, "to_function_declaration")
        ]

    async def execute_tool(
        self,
        name: str,
        parameters: dict[str, Any],
        run_id: str | None = None,
        db_client: Any = None,
    ) -> NexusToolResult:
        return await execute_tool_with_db(
            self, name, parameters, run_id=run_id, db_client=db_client
        )

    def get_diagnostic_tools(self, as_langchain: bool = False) -> list[Any]:
        if as_langchain:
            return self.get_read_only_tools_as_langchain()
        return [
            t
            for t in self._tools.values()
            if getattr(t, "risk_level", None) == ToolRiskLevel.L0_OBSERVE
        ]

    def get_remediation_tools(self, as_langchain: bool = False) -> list[Any]:
        tools = [
            t
            for t in self._tools.values()
            if getattr(t, "risk_level", None) != ToolRiskLevel.L0_OBSERVE
        ]
        return [t.as_langchain_tool() for t in tools] if as_langchain else tools


default_registry = ToolRegistry()


def get_tool_registry() -> ToolRegistry:
    """Return the global singleton ToolRegistry."""
    return default_registry
