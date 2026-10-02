"""Data models for action state snapshots and rollbacks."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class PreActionState:
    """Snapshot of resource state captured before a healing action executes."""

    action_type: str
    target_namespace: str
    target_name: str
    captured_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    state_data: dict[str, Any] = field(default_factory=dict)

    @property
    def rollback_key(self) -> str:
        return f"{self.action_type}::{self.target_namespace}/{self.target_name}"
