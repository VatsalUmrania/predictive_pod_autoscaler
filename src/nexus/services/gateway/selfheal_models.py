"""
Sub-models and environment resolution for selfheal.yaml configuration.
"""

from __future__ import annotations

import logging
import os
import re

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

_ENV_VAR_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _resolve_env(value: str) -> str:
    """Replace ${VAR} tokens with os.environ values."""

    def _sub(m: re.Match) -> str:
        key = m.group(1)
        resolved = os.environ.get(key, "")
        if not resolved:
            logger.warning(f"[SelfhealConfig] Unresolved env var: ${{{key}}}")
        return resolved

    return _ENV_VAR_RE.sub(_sub, value)


class HealingPolicy(BaseModel):
    """Governance constraints declared by the developer."""

    auto_rollback: bool = True
    max_auto_actions_per_hour: int = Field(default=10, ge=0, le=100)
    require_approval_for: list[str | dict[str, str]] = Field(
        default_factory=list,
        description=(
            "List of scenario names that require human approval. "
            "Strings like 'database_migrations', or dicts like {'scaling_above': '10x'}."
        ),
    )
    never_shed_routes: list[str] = Field(
        default_factory=list,
        description="Routes that should never be load-shed, even under extreme pressure.",
    )

    def requires_approval(self, scenario: str) -> bool:
        """Check whether a scenario requires human approval."""
        for item in self.require_approval_for:
            if isinstance(item, str) and item == scenario:
                return True
            if isinstance(item, dict) and scenario in item:
                return True
        return False

    def scaling_approval_threshold(self) -> float | None:
        """
        Return the replica-multiple above which scaling requires approval.
        e.g. {'scaling_above': '10x'} → 10.0
        """
        for item in self.require_approval_for:
            if isinstance(item, dict) and "scaling_above" in item:
                val = item["scaling_above"]
                try:
                    return float(str(val).rstrip("xX"))
                except ValueError:
                    pass
        return None


class PredictiveConfig(BaseModel):
    """Per-app predictive layer configuration."""

    traffic_spike_tables: list[str] = Field(
        default_factory=list,
        description="DB table names to watch for traffic spike prediction.",
    )
    pre_scale_threshold: float = Field(
        default=2.5,
        ge=1.1,
        le=20.0,
        description="Replica multiplier at which pre-scaling is triggered (e.g. 2.5 = 2.5x RPS).",
    )
    spike_indicator_queries: list[str] = Field(
        default_factory=list,
        description="SQL fragments that, when detected, indicate an imminent traffic spike.",
    )


class NotificationsConfig(BaseModel):
    """Slack and paging configuration."""

    slack_webhook: str = Field(default="", description="Slack incoming webhook URL.")
    page_sre_after: int = Field(
        default=3,
        ge=1,
        le=20,
        description="Number of failed healing attempts before SRE paging.",
    )
    notify_on_heal: bool = True
    notify_on_prescale: bool = True

    @field_validator("slack_webhook", mode="before")
    @classmethod
    def resolve_slack_webhook(cls, v: str) -> str:
        return _resolve_env(str(v)) if v else ""
