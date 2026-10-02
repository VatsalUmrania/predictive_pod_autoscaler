"""
NEXUS selfheal.yaml Config Schema
===================================
Pydantic schema for the developer-facing selfheal.yaml contract file.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from nexus.integration.selfheal_models import (
    HealingPolicy,
    NotificationsConfig,
    PredictiveConfig,
    _resolve_env,
)

logger = logging.getLogger(__name__)

__all__ = [
    "HealingPolicy",
    "PredictiveConfig",
    "NotificationsConfig",
    "SelfhealConfig",
    "load_selfheal_config",
    "_resolve_env",
]


class SelfhealConfig(BaseModel):
    """
    Root schema for selfheal.yaml.

    This is the developer's contract with NEXUS — everything they need to
    declare to get full self-healing + predictive behaviour for their app.
    """

    app: str = Field(
        ...,
        min_length=1,
        description="Application name (used for token lookup + incident labelling).",
    )
    tier: str = Field(
        default="production", description="Deployment tier: production | staging | dev"
    )
    critical_routes: list[str] = Field(default_factory=list)
    healing_policy: HealingPolicy = Field(default_factory=HealingPolicy)
    predictive: PredictiveConfig = Field(default_factory=PredictiveConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)

    # Populated by NEXUS at load time — not present in the YAML itself
    _token: str | None = None
    _source_path: Path | None = None

    @field_validator("app", mode="before")
    @classmethod
    def slugify_app(cls, v: str) -> str:
        """Strip whitespace; replace spaces with hyphens."""
        return str(v).strip().replace(" ", "-")

    @field_validator("tier", mode="before")
    @classmethod
    def validate_tier(cls, v: str) -> str:
        valid = {"production", "staging", "dev", "development", "preview"}
        v = str(v).strip().lower()
        if v not in valid:
            logger.warning(f"[SelfhealConfig] Unknown tier '{v}' — treating as 'dev'")
        return v

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


def load_selfheal_config(repo_path: str | Path) -> SelfhealConfig | None:
    """
    Load and validate selfheal.yaml from the given repo root.

    Returns None if the file doesn't exist (app hasn't opted in yet).
    Raises pydantic.ValidationError if the YAML is malformed.
    """
    path = Path(repo_path) / "selfheal.yaml"
    if not path.exists():
        path = Path(repo_path) / "selfheal.yml"
    if not path.exists():
        return None

    try:
        raw = path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw) or {}
    except Exception as exc:
        logger.error(f"[SelfhealConfig] Failed to read {path}: {exc}")
        return None

    try:
        cfg = SelfhealConfig.model_validate(data)
        cfg._source_path = path
        logger.info(
            f"[SelfhealConfig] Loaded for app='{cfg.app}' tier='{cfg.tier}' "
            f"tables={cfg.predictive.traffic_spike_tables}"
        )
        return cfg
    except Exception as exc:
        logger.error(f"[SelfhealConfig] Validation failed for {path}: {exc}")
        return None
