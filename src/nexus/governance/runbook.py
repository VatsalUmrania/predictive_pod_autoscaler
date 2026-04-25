"""
NEXUS Runbook Schema + Library
================================
Pydantic models for runbook definitions and a directory-backed loader
that validates, indexes, and hot-reloads runbook YAML files.

A runbook is a structured, auditable healing procedure:
    trigger       — event signal types + severity gate + conditions
    pre_checks    — assertions that must pass BEFORE execution
    actions       — ordered healing steps
    post_checks   — SLO assertions to verify healing worked
    rollback      — undo operations executed if post_checks fail

RunbookLibrary:
    Scans a directory for runbook_*.yaml files
    Validates each against the Pydantic schema
    Provides find_matching(event) for O(n) trigger lookup
    Supports hot-reload (call reload() after runbook changes)
"""

from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field

from nexus.bus.incident_event import HealingLevel, IncidentEvent

logger = logging.getLogger(__name__)

# Severity ordering for minimum-severity filtering
_SEV_ORDER = ["info", "warning", "critical", "emergency"]


# ──────────────────────────────────────────────────────────────────────────────
# Enums
# ──────────────────────────────────────────────────────────────────────────────

class BlastRadius(str, Enum):
    NONE               = "none"
    SINGLE_POD         = "single_pod"
    SINGLE_DEPLOYMENT  = "single_deployment"
    SINGLE_DATABASE    = "single_database"
    CLUSTER_DNS        = "cluster_dns"
    CLUSTER_WIDE       = "cluster_wide"


# ──────────────────────────────────────────────────────────────────────────────
# Sub-models
# ──────────────────────────────────────────────────────────────────────────────

class PreCheck(BaseModel):
    """Assertion that must pass before executing the runbook's actions."""
    type: str = "prometheus_query"       # "prometheus_query" | "event_field" | "k8s_resource"
    query: Optional[str] = None          # PromQL query (type=prometheus_query)
    field: Optional[str] = None          # Event context field (type=event_field)
    operator: str = "gt"                 # gt | gte | lt | lte | eq | ne
    threshold: Optional[float] = None    # Numeric comparison value
    value: Optional[Any] = None          # Non-numeric comparison value
    description: Optional[str] = None


class PostCheck(BaseModel):
    """SLO assertion that validates healing succeeded."""
    metric_query: Optional[str] = None   # Primary field name (YAML)
    query: Optional[str] = None          # Alias
    operator: str = "lt"
    threshold: float = 0.0
    window_seconds: int = Field(60, ge=1)
    timeout_seconds: int = Field(120, ge=10)
    description: Optional[str] = None

    @property
    def effective_query(self) -> Optional[str]:
        return self.metric_query or self.query


class RunbookAction(BaseModel):
    """A single discrete action in the healing sequence."""
    type: str                                                # Action type key
    description: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)
    abort_on_failure: bool = True                           # Stop runbook on this action's failure
    condition: Optional[str] = None                         # Optional guard expression (future use)


class RunbookTrigger(BaseModel):
    """Conditions under which a runbook fires."""
    signal_types: List[str] = Field(default_factory=list)
    severity_minimum: str = "warning"
    conditions: List[Dict[str, Any]] = Field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# Runbook
# ──────────────────────────────────────────────────────────────────────────────

class Runbook(BaseModel):
    """
    A complete, validated runbook definition.

    Loaded from YAML and validated against this schema.
    Immutable after construction — hot-reload replaces the library,
    not individual runbook objects.
    """

    id: str
    version: str = "1.0.0"
    description: str = ""
    failure_class: str = ""
    healing_level: int = Field(0, ge=0, le=3)
    trigger: RunbookTrigger
    pre_checks: List[PreCheck] = Field(default_factory=list)
    actions: List[RunbookAction] = Field(default_factory=list)
    post_checks: List[PostCheck] = Field(default_factory=list)
    rollback_if_post_check_fails: bool = True
    rollback_actions: List[RunbookAction] = Field(default_factory=list)
    cooldown_seconds: int = Field(300, ge=0)
    blast_radius: str = "unknown"

    @property
    def level(self) -> HealingLevel:
        """HealingLevel enum for this runbook."""
        return HealingLevel(self.healing_level)

    def matches_event(self, event: IncidentEvent) -> bool:
        """Returns True if this runbook's trigger conditions match the event."""
        # Signal type gate
        if self.trigger.signal_types:
            if event.signal_type not in self.trigger.signal_types:
                return False

        # Severity minimum gate
        try:
            min_idx = _SEV_ORDER.index(self.trigger.severity_minimum.lower())
            evt_idx = _SEV_ORDER.index(event.severity.lower())
            if evt_idx < min_idx:
                return False
        except ValueError:
            pass  # Unknown severity — let it through

        return True


# ──────────────────────────────────────────────────────────────────────────────
# RunbookLibrary
# ──────────────────────────────────────────────────────────────────────────────

class RunbookLibrary:
    """
    Loads, validates, and indexes all runbook YAML files from a directory.

    Exposes:
        find_matching(event) → List[Runbook]  sorted by healing_level ascending
        get(runbook_id)      → Optional[Runbook]
        reload()             → None              hot-reload from disk

    YAML format:
        runbook:
          id: "runbook_pod_crashloop_v1"
          healing_level: 1
          trigger:
            signal_types: ["pod_crashloop"]
          actions:
            - type: restart_pod
    """

    def __init__(self, runbook_dir: Path):
        self._dir = runbook_dir
        self._runbooks: Dict[str, Runbook] = {}
        self._load_all()

    # ── Loading ───────────────────────────────────────────────────────────────

    def _load_all(self) -> None:
        if not self._dir.exists():
            logger.warning(f"[RunbookLibrary] Directory not found: {self._dir}")
            return

        loaded = 0
        for path in sorted(self._dir.glob("runbook_*.yaml")):
            rb = self._load_one(path)
            if rb:
                self._runbooks[rb.id] = rb
                loaded += 1

        logger.info(f"[RunbookLibrary] Loaded {loaded} runbooks from {self._dir}")

    def _load_one(self, path: Path) -> Optional[Runbook]:
        try:
            with open(path, encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            rb_data = raw.get("runbook")
            if not isinstance(rb_data, dict):
                logger.warning(f"[RunbookLibrary] No 'runbook' key in {path.name}")
                return None
            rb = Runbook.model_validate(rb_data)
            logger.debug(f"[RunbookLibrary]  ✓ {rb.id}  L{rb.healing_level}  blast={rb.blast_radius}")
            return rb
        except Exception as exc:
            logger.error(f"[RunbookLibrary] Failed to load {path.name}: {exc}")
            return None

    def reload(self) -> None:
        """Hot-reload all runbooks from disk without restarting."""
        self._runbooks.clear()
        self._load_all()

    # ── Lookup ────────────────────────────────────────────────────────────────

    def find_matching(self, event: IncidentEvent) -> List[Runbook]:
        """
        Return all runbooks whose trigger matches the event.
        Results are sorted by healing_level ascending (L0 first — least invasive).
        """
        return sorted(
            [rb for rb in self._runbooks.values() if rb.matches_event(event)],
            key=lambda r: r.healing_level,
        )

    def get(self, runbook_id: str) -> Optional[Runbook]:
        return self._runbooks.get(runbook_id)

    def all(self) -> List[Runbook]:
        return list(self._runbooks.values())

    def count(self) -> int:
        return len(self._runbooks)

    def ids(self) -> List[str]:
        return sorted(self._runbooks.keys())
