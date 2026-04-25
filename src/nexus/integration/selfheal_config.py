"""
NEXUS selfheal.yaml Config Schema
===================================
Pydantic schema for the developer-facing selfheal.yaml contract file.

Developers commit this file to their repo root. On every push, GitAgent
reads it, validates against this schema, auto-issues a SELFHEAL_TOKEN for
new apps, and propagates settings into the relevant NEXUS planes:

    RunbookExecutor  ← max_auto_actions_per_hour, require_approval_for
    DBTrafficCorrelator ← traffic_spike_tables, pre_scale_threshold
    Prescaler        ← pre_scale_threshold (per-app override)
    Notifier         ← slack_webhook, page_sre_after

${ENV_VAR} references in string fields are resolved from os.environ at
load time. Unresolved vars produce a warning and are left as empty strings.

Example selfheal.yaml:
    app: checkout-service
    tier: production

    critical_routes:
      - /api/checkout
      - /api/payment
      - /api/orders

    healing_policy:
      auto_rollback: true
      max_auto_actions_per_hour: 10
      require_approval_for:
        - database_migrations
        - environment_variable_changes
        - scaling_above: 10x

    predictive:
      traffic_spike_tables:
        - orders
        - products
        - inventory
      pre_scale_threshold: 2.5

    notifications:
      slack_webhook: ${SLACK_WEBHOOK}
      page_sre_after: 3
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml
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


# ──────────────────────────────────────────────────────────────────────────────
# Sub-models
# ──────────────────────────────────────────────────────────────────────────────

class HealingPolicy(BaseModel):
    """Governance constraints declared by the developer."""

    auto_rollback:              bool                     = True
    max_auto_actions_per_hour:  int                      = Field(10, ge=0, le=100)
    require_approval_for:       List[Union[str, Dict[str, str]]] = Field(
        default_factory=list,
        description=(
            "List of scenario names that require human approval. "
            "Strings like 'database_migrations', or dicts like {'scaling_above': '10x'}."
        ),
    )
    never_shed_routes:          List[str] = Field(
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

    def scaling_approval_threshold(self) -> Optional[float]:
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

    traffic_spike_tables: List[str] = Field(
        default_factory=list,
        description="DB table names to watch for traffic spike prediction.",
    )
    pre_scale_threshold: float = Field(
        2.5, ge=1.1, le=20.0,
        description="Replica multiplier at which pre-scaling is triggered (e.g. 2.5 = 2.5x RPS).",
    )
    spike_indicator_queries: List[str] = Field(
        default_factory=list,
        description="SQL fragments that, when detected, indicate an imminent traffic spike.",
    )


class NotificationsConfig(BaseModel):
    """Slack and paging configuration."""

    slack_webhook:   str = Field("", description="Slack incoming webhook URL.")
    page_sre_after:  int = Field(3, ge=1, le=20,
                                 description="Number of failed healing attempts before SRE paging.")
    notify_on_heal:  bool = True
    notify_on_prescale: bool = True

    @field_validator("slack_webhook", mode="before")
    @classmethod
    def resolve_slack_webhook(cls, v: str) -> str:
        return _resolve_env(str(v)) if v else ""


# ──────────────────────────────────────────────────────────────────────────────
# Root schema
# ──────────────────────────────────────────────────────────────────────────────

class SelfhealConfig(BaseModel):
    """
    Root schema for selfheal.yaml.

    This is the developer's contract with NEXUS — everything they need to
    declare to get full self-healing + predictive behaviour for their app.
    """

    app:             str = Field(..., min_length=1, description="Application name (used for token lookup + incident labelling).")
    tier:            str = Field("production", description="Deployment tier: production | staging | dev")
    critical_routes: List[str]           = Field(default_factory=list)
    healing_policy:  HealingPolicy       = Field(default_factory=HealingPolicy)
    predictive:      PredictiveConfig    = Field(default_factory=PredictiveConfig)
    notifications:   NotificationsConfig = Field(default_factory=NotificationsConfig)

    # Populated by NEXUS at load time — not present in the YAML itself
    _token:        Optional[str]        = None
    _source_path:  Optional[Path]       = None

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

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()


# ──────────────────────────────────────────────────────────────────────────────
# Loader
# ──────────────────────────────────────────────────────────────────────────────

def load_selfheal_config(repo_path: Union[str, Path]) -> Optional[SelfhealConfig]:
    """
    Load and validate selfheal.yaml from the given repo root.

    Returns None if the file doesn't exist (app hasn't opted in yet).
    Raises pydantic.ValidationError if the YAML is malformed.

    Usage:
        cfg = load_selfheal_config("/path/to/my-app")
        if cfg:
            print(cfg.app, cfg.predictive.traffic_spike_tables)
    """
    path = Path(repo_path) / "selfheal.yaml"
    if not path.exists():
        path = Path(repo_path) / "selfheal.yml"   # also accept .yml
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
