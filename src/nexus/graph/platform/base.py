"""
NEXUS Multi-Platform Abstraction
=================================
Defines the foundational contracts, data models, and Service Provider Interface
(SPI) for multi-platform infrastructure observability and remediation.

Designed to support Kubernetes and AWS natively, while allowing seamless
addition of future platforms (e.g. GCP, Azure, Bare-Metal, Nomad) via
BasePlatformAdapter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from nexus.tools.base import NexusTool, NexusToolResult


class PlatformType(str, Enum):
    KUBERNETES = "kubernetes"
    AWS = "aws"
    GCP = "gcp"
    AZURE = "azure"
    BAREMETAL = "baremetal"
    CUSTOM = "custom"


class TargetResource(BaseModel):
    """Normalized identifier for an infrastructure target across any platform."""

    platform: str = Field(..., description="Platform identifier (e.g. 'kubernetes', 'aws')")
    namespace: str = Field(default="default", description="Namespace, region, account, or project scope")
    name: str = Field(..., description="Resource name (e.g. deployment name, lambda name, queue name)")
    kind: str = Field(default="unknown", description="Resource kind (e.g. 'deployment', 'pod', 'lambda', 'sqs')")
    arn_or_uri: str | None = Field(default=None, description="Platform-specific global URI or ARN if applicable")
    labels: dict[str, str] = Field(default_factory=dict, description="Resource labels or tags")

    @property
    def target_key(self) -> str:
        """Standardized unique string key: namespace/name or platform:namespace/name."""
        return f"{self.namespace}/{self.name}"

    def __str__(self) -> str:
        return f"[{self.platform.upper()}:{self.kind}] {self.target_key}"


class PlatformTelemetry(BaseModel):
    """Normalized snapshot of telemetry and diagnostic signals gathered from a platform."""

    target: TargetResource
    captured_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metrics: dict[str, Any] = Field(default_factory=dict, description="Recent metrics (CPU, memory, error rates, latencies)")
    recent_logs: list[str] = Field(default_factory=list, description="Recent log lines or events")
    live_config: dict[str, Any] = Field(default_factory=dict, description="Live specs/limits/environment variables")
    health_status: str = Field(default="unknown", description="Current target health: healthy, degraded, failing, oomkilled")
    raw_signals: list[dict[str, Any]] = Field(default_factory=list, description="Correlated incoming alert/incident events")


class LiveStateSnapshot(BaseModel):
    """Current state of a target resource before any mutation is applied."""

    target: TargetResource
    captured_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    state_attributes: dict[str, Any] = Field(default_factory=dict, description="Exact pre-mutation properties")
    exists: bool = True
    is_failing: bool = False
    failure_details: str | None = None


class RollbackSnapshot(BaseModel):
    """Structured pre-mutation snapshot stored for deterministic undo operations."""

    snapshot_id: str
    target: TargetResource
    action_name: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    pre_mutation_state: dict[str, Any] = Field(default_factory=dict)
    rollback_tool: str
    rollback_parameters: dict[str, Any] = Field(default_factory=dict)
    description: str = ""


class HealthCheckResult(BaseModel):
    """Post-remediation health verification verdict."""

    healthy: bool
    slo_restored: bool
    metrics: dict[str, Any] = Field(default_factory=dict)
    failure_reason: str | None = None
    verification_time: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    details: str = ""


class BasePlatformAdapter(ABC):
    """
    Abstract Service Provider Interface (SPI) for all platform adapters.

    To support a new platform (e.g. GCP, Azure, Bare-Metal), implement this class
    and register it with PlatformRegistry. The LangGraph workflow requires NO modifications.
    """

    @property
    @abstractmethod
    def platform_id(self) -> str:
        """Unique string identifier for the platform (e.g. 'kubernetes', 'aws')."""
        pass

    @abstractmethod
    def can_handle(self, event_or_target: dict[str, Any] | TargetResource) -> bool:
        """Determine if this adapter can handle the given incident event or target."""
        pass

    @abstractmethod
    def detect_target(self, events: list[dict[str, Any]]) -> TargetResource:
        """Extract and normalize the primary target resource from incoming incident events."""
        pass

    @abstractmethod
    async def collect_telemetry(self, target: TargetResource) -> PlatformTelemetry:
        """
        Deterministically collect logs, metrics, and live config for the target.
        Used by the LangGraph CollectTelemetry node (replacing unconstrained ReAct loops).
        """
        pass

    @abstractmethod
    async def inspect_live_state(self, target: TargetResource) -> LiveStateSnapshot:
        """Inspect the current live state of the target resource before mutation."""
        pass

    @abstractmethod
    def get_tools(self) -> list[NexusTool]:
        """Return the catalog of registered NexusTools for this platform."""
        pass

    @abstractmethod
    async def execute_action(
        self, tool_name: str, parameters: dict[str, Any]
    ) -> NexusToolResult:
        """Execute a diagnostic or remediation action on the platform."""
        pass

    @abstractmethod
    async def capture_snapshot(
        self, target: TargetResource, tool_name: str, parameters: dict[str, Any]
    ) -> RollbackSnapshot | None:
        """
        Capture pre-mutation state so this action can be deterministically rolled back.
        Returns None for read-only or non-reversible actions.
        """
        pass

    @abstractmethod
    async def execute_rollback(self, snapshot: RollbackSnapshot) -> NexusToolResult:
        """Deterministically apply the rollback snapshot to restore pre-mutation state."""
        pass

    @abstractmethod
    async def verify_health(
        self, target: TargetResource, plan_step: dict[str, Any]
    ) -> HealthCheckResult:
        """
        Verify post-execution health and SLO restoration for the target.
        """
        pass
