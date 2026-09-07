"""
NEXUS Graph Platform Subsystem
==============================
Provides extensible multi-platform contracts, adapters, and dynamic registry.
"""

from __future__ import annotations

from nexus.graph.platform.aws import AWSPlatformAdapter
from nexus.graph.platform.base import (
    BasePlatformAdapter,
    HealthCheckResult,
    LiveStateSnapshot,
    PlatformTelemetry,
    PlatformType,
    RollbackSnapshot,
    TargetResource,
)
from nexus.graph.platform.k8s import K8sPlatformAdapter
from nexus.graph.platform.registry import PlatformRegistry, get_platform_registry

__all__ = [
    "BasePlatformAdapter",
    "PlatformType",
    "TargetResource",
    "PlatformTelemetry",
    "LiveStateSnapshot",
    "RollbackSnapshot",
    "HealthCheckResult",
    "PlatformRegistry",
    "get_platform_registry",
    "K8sPlatformAdapter",
    "AWSPlatformAdapter",
]
