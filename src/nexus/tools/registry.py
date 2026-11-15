"""
NEXUS Unified Tool Registry
===========================
Central catalog managing discovery, schema reflection, read/write separation,
and execution for universal AWS, Kubernetes, and MCP tools.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from nexus.tools.aws.universal import (
    AWSInspectInput,
    AWSMutateInput,
    AWSQueryTelemetryInput,
    execute_aws_inspect,
    execute_aws_mutate,
    execute_aws_query_telemetry,
)
from nexus.tools.base import NexusTool, NexusToolResult, ToolDomain, ToolRiskLevel
from nexus.tools.k8s.universal import (
    K8sGetLogsInput,
    K8sInspectResourceInput,
    K8sMutateResourceInput,
    execute_k8s_get_logs,
    execute_k8s_inspect,
    execute_k8s_mutate,
)
from nexus.tools.registry_helpers import execute_tool_with_db, register_legacy_tools

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Registry managing read-only and mutating tools with explicit separation."""

    def __init__(self) -> None:
        self._tools: dict[str, NexusTool] = {}
        self._universal_callables: dict[
            str, tuple[Callable[..., Any], type[BaseModel], bool, str]
        ] = {}
        self._register_universal_tools()
        register_legacy_tools(self)

    def _register_universal_tools(self) -> None:
        self.register_function(
            "aws_inspect_resource",
            execute_aws_inspect,
            AWSInspectInput,
            is_mutating=False,
            provider="aws",
        )
        self.register_function(
            "aws_query_telemetry",
            execute_aws_query_telemetry,
            AWSQueryTelemetryInput,
            is_mutating=False,
            provider="aws",
        )
        self.register_function(
            "aws_mutate_resource",
            execute_aws_mutate,
            AWSMutateInput,
            is_mutating=True,
            provider="aws",
        )
        self.register_function(
            "k8s_inspect_resource",
            execute_k8s_inspect,
            K8sInspectResourceInput,
            is_mutating=False,
            provider="kubernetes",
        )
        self.register_function(
            "k8s_get_logs",
            execute_k8s_get_logs,
            K8sGetLogsInput,
            is_mutating=False,
            provider="kubernetes",
        )
        self.register_function(
            "k8s_mutate_resource",
            execute_k8s_mutate,
            K8sMutateResourceInput,
            is_mutating=True,
            provider="kubernetes",
        )

    def register_function(
        self,
        name: str,
        fn: Callable[..., Any],
        args_schema: type[BaseModel],
        is_mutating: bool,
        provider: str,
    ) -> None:
        self._universal_callables[name] = (fn, args_schema, is_mutating, provider)

    def register(self, tool: NexusTool) -> None:
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)
        self._universal_callables.pop(name, None)

    def get(self, name: str) -> NexusTool | None:
        return self._tools.get(name)

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

    def get_read_only_tools_as_langchain(self) -> list[StructuredTool]:
        tools: list[StructuredTool] = []
        for name, (fn, schema, is_mut, _) in self._universal_callables.items():
            if not is_mut:
                tools.append(
                    StructuredTool.from_function(
                        func=fn,
                        coroutine=fn if inspect.iscoroutinefunction(fn) else None,
                        name=name,
                        description=getattr(schema, "__doc__", "") or f"Execute {name}",
                        args_schema=schema,
                    )
                )
        for t in self._tools.values():
            if getattr(t, "risk_level", None) == ToolRiskLevel.L0_OBSERVE:
                tools.append(t.as_langchain_tool())
        return tools

    def get_mutating_tool_schemas(self) -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = [
            {
                "name": n,
                "provider": p,
                "is_mutating": True,
                "parameters": s.model_json_schema(),
            }
            for n, (_, s, is_mut, p) in self._universal_callables.items()
            if is_mut
        ]
        for t in self._tools.values():
            if getattr(t, "risk_level", None) != ToolRiskLevel.L0_OBSERVE:
                sch = getattr(t, "args_schema", None)
                params = (
                    sch.model_json_schema()
                    if sch and hasattr(sch, "model_json_schema")
                    else {}
                )
                schemas.append(
                    {
                        "name": t.name,
                        "provider": getattr(t.domain, "value", "unknown"),
                        "is_mutating": True,
                        "parameters": params,
                    }
                )
        return schemas

    def get_read_only_tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": n,
                "provider": p,
                "is_mutating": False,
                "parameters": s.model_json_schema(),
            }
            for n, (_, s, is_mut, p) in self._universal_callables.items()
            if not is_mut
        ]

    async def execute_read_only(self, tool_name: str, tool_args: dict[str, Any]) -> Any:
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
