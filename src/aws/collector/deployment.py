"""
Phase 4 — Collector: Recent Deployment Info
=============================================
Gathers recent Lambda version/alias deployment history.
Answers: "Was this function recently deployed? By whom? What changed?"

This is critical context — most production incidents follow a deploy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class DeploymentInfo:
    """Represents a Lambda deployment event."""
    function_name: str
    version: str
    description: str
    last_modified: str
    runtime: str
    memory_mb: int
    timeout_seconds: int
    code_sha256: str


@dataclass
class DeploymentContext:
    """Full deployment context for a Lambda function."""
    function_name: str
    current_config: dict[str, Any] = field(default_factory=dict)
    published_versions: list[DeploymentInfo] = field(default_factory=list)
    active_aliases: list[dict] = field(default_factory=list)

    @property
    def version_count(self) -> int:
        return len(self.published_versions)

    @property
    def most_recent_version(self) -> DeploymentInfo | None:
        return self.published_versions[-1] if self.published_versions else None

    @property
    def previous_version(self) -> DeploymentInfo | None:
        return self.published_versions[-2] if len(self.published_versions) >= 2 else None


def collect_lambda_deployment(lambda_client, function_name: str) -> DeploymentContext:
    """
    Collect full deployment context for a Lambda function:
    - Current configuration (memory, timeout, runtime, env vars keys)
    - Last 10 published versions
    - Active aliases
    """
    config = {}
    versions: list[DeploymentInfo] = []
    aliases: list[dict] = []

    # Current config
    try:
        resp = lambda_client.get_function_configuration(FunctionName=function_name)
        config = {
            "memory_mb": resp.get("MemorySize", 128),
            "timeout_seconds": resp.get("Timeout", 3),
            "runtime": resp.get("Runtime", "unknown"),
            "last_modified": resp.get("LastModified", ""),
            "code_sha256": resp.get("CodeSha256", ""),
            # Only expose env var KEYS, not values (security)
            "env_var_keys": list(resp.get("Environment", {}).get("Variables", {}).keys()),
            "layers": [
                l.get("Arn", "")
                for l in resp.get("Layers", [])
            ],
        }
    except Exception as exc:
        logger.debug(f"[Deployment] get_config({function_name}): {exc}")

    # Published versions
    try:
        resp = lambda_client.list_versions_by_function(
            FunctionName=function_name, MaxItems=10
        )
        for v in resp.get("Versions", []):
            if v.get("Version") == "$LATEST":
                continue
            versions.append(DeploymentInfo(
                function_name=function_name,
                version=v.get("Version", ""),
                description=v.get("Description", "")[:100],
                last_modified=v.get("LastModified", ""),
                runtime=v.get("Runtime", ""),
                memory_mb=v.get("MemorySize", 128),
                timeout_seconds=v.get("Timeout", 3),
                code_sha256=v.get("CodeSha256", ""),
            ))
        # Sort oldest → newest
        versions.sort(key=lambda v: v.last_modified)
    except Exception as exc:
        logger.debug(f"[Deployment] list_versions({function_name}): {exc}")

    # Aliases
    try:
        resp = lambda_client.list_aliases(FunctionName=function_name)
        aliases = [
            {
                "name": a.get("Name", ""),
                "function_version": a.get("FunctionVersion", ""),
                "description": a.get("Description", "")[:100],
            }
            for a in resp.get("Aliases", [])
        ]
    except Exception as exc:
        logger.debug(f"[Deployment] list_aliases({function_name}): {exc}")

    return DeploymentContext(
        function_name=function_name,
        current_config=config,
        published_versions=versions,
        active_aliases=aliases,
    )


def to_context_dict(ctx: DeploymentContext) -> dict[str, Any]:
    """Convert DeploymentContext to a plain dict for the LLM prompt."""
    recent = [
        {
            "version": v.version,
            "last_modified": v.last_modified,
            "description": v.description,
            "memory_mb": v.memory_mb,
            "timeout_seconds": v.timeout_seconds,
        }
        for v in ctx.published_versions[-5:]   # last 5 only
    ]
    return {
        "current_config": ctx.current_config,
        "recent_versions": recent,
        "version_count": ctx.version_count,
        "active_aliases": ctx.active_aliases,
    }
