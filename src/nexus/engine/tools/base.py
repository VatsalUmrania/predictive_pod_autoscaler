"""
NEXUS Unified Tool Contracts
============================
Core abstractions and metadata for all diagnostic and remediation tools
across Kubernetes and AWS environments.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ToolDomain(str, Enum):
    K8S = "kubernetes"
    AWS = "aws"
    SYSTEM = "system"


class ToolRiskLevel(str, Enum):
    L0_OBSERVE = "L0_OBSERVE"  # Read-only telemetry, logs, status
    L1_SAFE = "L1_SAFE_AUTOMATED"  # Reversible/no-regret actions (e.g. pod restart)
    L2_MUTATE_APPROVAL = (
        "L2_MUTATING_APPROVAL"  # Bounded state modification (scale, memory patch)
    )
    L3_DESTRUCTIVE = (
        "L3_DESTRUCTIVE"  # Major changes (rollout undo, lambda alias rollback)
    )


class NexusToolResult(BaseModel):
    success: bool
    data: Any | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "metadata": self.metadata,
        }


class NexusTool(ABC):
    """Abstract base class for all NEXUS tools."""

    name: str
    description: str
    domain: ToolDomain
    risk_level: ToolRiskLevel
    args_schema: type[BaseModel]
    blast_radius: str = "service"
    is_destructive: bool = False
    allows_unbacked_rollback: bool = False

    def as_langchain_tool(self) -> Any:
        """Wrap this NexusTool into a LangChain StructuredTool."""
        from langchain_core.tools import StructuredTool

        async def _coroutine(**kwargs: Any) -> dict[str, Any]:
            res = await self.execute(**kwargs)
            return res.to_dict()

        def _sync(**kwargs: Any) -> dict[str, Any]:
            import asyncio

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop and loop.is_running():
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    return pool.submit(asyncio.run, _coroutine(**kwargs)).result()
            return asyncio.run(_coroutine(**kwargs))

        tool = StructuredTool.from_function(
            func=_sync,
            coroutine=_coroutine,
            name=self.name,
            description=f"[{self.domain.value.upper()} | {self.risk_level.value}] {self.description}",
            args_schema=self.args_schema,
        )
        tool.metadata = {
            "blast_radius": self.blast_radius,
            "is_destructive": self.is_destructive,
            "allows_unbacked_rollback": self.allows_unbacked_rollback,
            "risk_level": self.risk_level.value,
            "domain": self.domain.value,
        }
        return tool

    @abstractmethod
    async def execute(self, **kwargs: Any) -> NexusToolResult:
        """Execute the tool with validated keyword arguments."""
        pass

    def get_rollback_action(self, **kwargs: Any) -> dict[str, Any] | None:
        """
        Return the inverse action definition if this action is reversible.
        Returns None for read-only or non-reversible actions.
        """
        return None

    def to_function_declaration(self) -> dict[str, Any]:
        """Return OpenAPI-compatible function declaration for Gemini/OpenAI."""
        schema = self.args_schema.model_json_schema()
        # Clean schema for tool calling
        schema.pop("title", None)
        return {
            "name": self.name,
            "description": f"[{self.domain.value.upper()} | {self.risk_level.value}] {self.description}",
            "parameters": {
                "type": "OBJECT",
                "properties": schema.get("properties", {}),
                "required": schema.get("required", []),
            },
        }
