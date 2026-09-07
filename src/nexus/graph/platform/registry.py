"""
NEXUS Platform Registry
=======================
Central registry managing infrastructure platform adapters.
Provides automatic platform detection from incident events/targets and supports
runtime registration of new platform adapters (GCP, Azure, Bare-Metal, etc.).
"""

from __future__ import annotations

import logging
from typing import Any

from nexus.graph.platform.base import BasePlatformAdapter, TargetResource

logger = logging.getLogger(__name__)


class PlatformRegistry:
    """Registry maintaining available platform adapters."""

    def __init__(self) -> None:
        self._adapters: dict[str, BasePlatformAdapter] = {}

    def register(self, adapter: BasePlatformAdapter) -> None:
        """Register a platform adapter instance."""
        self._adapters[adapter.platform_id.lower()] = adapter
        logger.info("[PlatformRegistry] Registered platform adapter: %s", adapter.platform_id)

    def get(self, platform_id: str) -> BasePlatformAdapter | None:
        """Get an adapter by platform identifier."""
        return self._adapters.get(platform_id.lower())

    def list_platforms(self) -> list[str]:
        """List all registered platform identifiers."""
        return list(self._adapters.keys())

    def resolve_adapter(
        self, event_or_target: dict[str, Any] | TargetResource | list[dict[str, Any]]
    ) -> BasePlatformAdapter:
        """
        Resolve the appropriate platform adapter for an event, target, or event list.
        Falls back to Kubernetes if ambiguous or not matched.
        """
        candidate = event_or_target
        if isinstance(candidate, list):
            candidate = candidate[0] if candidate else {}

        # 1. Check if candidate explicitly provides platform
        if isinstance(candidate, TargetResource):
            adapter = self.get(candidate.platform)
            if adapter:
                return adapter

        if isinstance(candidate, dict):
            explicit_plat = candidate.get("platform")
            if explicit_plat and isinstance(explicit_plat, str):
                adapter = self.get(explicit_plat)
                if adapter:
                    return adapter

        # 2. Query each registered adapter's can_handle
        for adapter in self._adapters.values():
            try:
                if adapter.can_handle(candidate):
                    return adapter
            except Exception as e:
                logger.debug("[PlatformRegistry] Adapter %s can_handle check error: %s", adapter.platform_id, e)

        # 3. Default fallback to kubernetes if registered, otherwise first available
        if "kubernetes" in self._adapters:
            return self._adapters["kubernetes"]
        if self._adapters:
            return next(iter(self._adapters.values()))

        raise RuntimeError("[PlatformRegistry] No platform adapters registered in registry")


# Global singleton registry
_GLOBAL_PLATFORM_REGISTRY: PlatformRegistry | None = None


def get_platform_registry() -> PlatformRegistry:
    """Return or initialize the global platform registry with default K8s and AWS adapters."""
    global _GLOBAL_PLATFORM_REGISTRY
    if _GLOBAL_PLATFORM_REGISTRY is None:
        registry = PlatformRegistry()
        # Auto-register Kubernetes and AWS adapters
        try:
            from nexus.graph.platform.k8s import K8sPlatformAdapter
            registry.register(K8sPlatformAdapter())
        except Exception as k8s_err:
            logger.warning("[PlatformRegistry] Failed to auto-register K8sPlatformAdapter: %s", k8s_err)

        try:
            from nexus.graph.platform.aws import AWSPlatformAdapter
            registry.register(AWSPlatformAdapter())
        except Exception as aws_err:
            logger.warning("[PlatformRegistry] Failed to auto-register AWSPlatformAdapter: %s", aws_err)

        _GLOBAL_PLATFORM_REGISTRY = registry

    return _GLOBAL_PLATFORM_REGISTRY
