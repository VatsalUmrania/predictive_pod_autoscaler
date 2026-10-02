"""The normalized NEXUS incident event model."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, cast

from pydantic import BaseModel, Field, field_validator

from nexus.bus.events.enums import AgentType, HealingLevel, Severity, SignalType


class IncidentEvent(BaseModel):
    """The normalized NEXUS incident event."""

    # Identity
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Source
    agent: AgentType | str
    signal_type: SignalType | str
    severity: Severity | str

    # Resource targeting
    namespace: str | None = None
    resource_name: str | None = None  # e.g. "payments-api"
    resource_kind: str | None = None  # e.g. "Deployment"

    # Correlation
    correlation_id: str | None = None  # Groups related events into one incident
    deploy_sha: str | None = None  # Ties event to a deployment
    parent_event_id: str | None = None  # For child / follow-up events

    # Payload
    context: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] | None = None

    # Advisory fields (agent can optionally suggest a path)
    suggested_runbook: str | None = None
    suggested_healing_level: HealingLevel | int | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    model_config = {"use_enum_values": True}

    @field_validator("timestamp", mode="before")
    @classmethod
    def ensure_utc(cls, v: Any) -> datetime:
        if isinstance(v, str):
            v = datetime.fromisoformat(v)
        if isinstance(v, datetime) and v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return cast(datetime, v)

    def nats_subject(self) -> str:
        """Returns the canonical NATS subject for this event."""
        return f"nexus.incidents.{self.agent}.{self.signal_type}"

    def to_nats_payload(self) -> bytes:
        """Serializes the event for NATS publish."""
        return self.model_dump_json().encode("utf-8")

    @classmethod
    def from_nats_payload(cls, payload: bytes) -> IncidentEvent:
        """Deserializes an event received from NATS."""
        return cls.model_validate_json(payload)

    def is_severity_at_least(self, minimum: str) -> bool:
        order = ["info", "warning", "critical", "emergency"]
        return order.index(self.severity) >= order.index(minimum)
