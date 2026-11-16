"""The normalized NEXUS incident event model."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, cast
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from nexus.bus.events.enums import (
    AgentType,
    HealingLevel,
    Severity,
    SignalKind,
    SignalSource,
    SignalType,
)


class SymptomSignal(BaseModel):
    signal_id: str = Field(default_factory=lambda: f"sig-{uuid4().hex[:12]}")
    source: SignalSource
    kind: SignalKind
    provider: str = Field(
        description="Cloud/platform provider, e.g., 'aws', 'kubernetes'"
    )
    resource_urn: str = Field(
        description="Universal resource identifier, e.g., ARN 'arn:aws:rds:us-east-1:123456789012:db:prod-pg' or 'k8s://prod/apps/v1/StatefulSet/redis'"
    )
    region_or_namespace: str = Field(default="default")
    symptom_summary: str = Field(
        description="Natural-language alert or anomaly summary"
    )
    raw_payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Raw CloudWatch/EventBridge/K8s/OTel JSON payload",
    )
    severity: Severity = Severity.HIGH
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

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
        return f"nexus.symptom.{self.provider}.{self.kind}"

    @property
    def workload_urn(self) -> str:
        return self.resource_urn

    @property
    def service_name(self) -> str:
        if "/" in self.resource_urn:
            return self.resource_urn.split("/")[-1]
        if ":" in self.resource_urn:
            return self.resource_urn.split(":")[-1]
        return self.resource_urn

    @property
    def namespace(self) -> str:
        return self.region_or_namespace

    @property
    def reason(self) -> str:
        if ":" in self.symptom_summary:
            return self.symptom_summary.split(":", 1)[0].strip()
        return "AnomalyEvent"

    @property
    def message(self) -> str:
        return self.symptom_summary

    def to_nats_payload(self) -> bytes:
        return self.model_dump_json().encode("utf-8")

    @classmethod
    def from_nats_payload(cls, payload: bytes) -> SymptomSignal:
        return cls.model_validate_json(payload)

    def to_incident_event(self) -> IncidentEvent:
        """Convert SymptomSignal to normalized IncidentEvent for workflow consumption."""
        parts = self.resource_urn.split("/") if "/" in self.resource_urn else []
        res_name = parts[-1] if parts else self.service_name
        res_kind = parts[-2] if len(parts) >= 2 else "Pod"
        provider = str(getattr(self.provider, "value", self.provider)).lower()
        agent = "k8s" if provider in ("kubernetes", "k8s") else provider
        sev = str(getattr(self.severity, "value", self.severity)).lower()
        if "." in sev:
            sev = sev.split(".")[-1]
        kind_str = str(getattr(self.kind, "value", self.kind))
        return IncidentEvent(
            event_id=self.signal_id,
            timestamp=self.timestamp,
            agent=agent,
            signal_type=kind_str,
            severity=sev,
            namespace=self.region_or_namespace,
            resource_name=res_name,
            resource_kind=res_kind,
            context={
                "summary": self.symptom_summary,
                "urn": self.resource_urn,
                "raw": self.raw_payload,
            },
            raw=self.raw_payload,
        )


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
        """Deserializes an event received from NATS with SymptomSignal fallback."""
        try:
            return cls.model_validate_json(payload)
        except Exception:
            import json

            try:
                data = json.loads(payload.decode("utf-8"))
            except Exception:
                raise
            if (
                "resource_urn" in data
                or "symptom_summary" in data
                or "signal_id" in data
            ):
                res_urn = str(data.get("resource_urn", ""))
                parts = res_urn.split("/") if "/" in res_urn else []
                res_name = (
                    parts[-1] if parts else str(data.get("target_service", "unknown"))
                )
                res_kind = parts[-2] if len(parts) >= 2 else "Pod"
                ns = (
                    data.get("region_or_namespace")
                    or data.get("namespace")
                    or "default"
                )
                provider = str(data.get("provider", "kubernetes")).lower()
                agent = "k8s" if provider in ("kubernetes", "k8s") else provider
                sev = str(data.get("severity", "warning")).lower()
                if "." in sev:
                    sev = sev.split(".")[-1]
                return cls(
                    event_id=str(data.get("signal_id") or uuid.uuid4()),
                    timestamp=datetime.now(timezone.utc),
                    agent=agent,
                    signal_type=str(data.get("kind", "error_log_burst")),
                    severity=sev,
                    namespace=str(ns),
                    resource_name=res_name,
                    resource_kind=res_kind,
                    context={
                        "summary": str(data.get("symptom_summary", "")),
                        "urn": res_urn,
                        "raw": data.get("raw_payload", {}),
                    },
                    raw=data.get("raw_payload") or data,
                )
            raise

    def is_severity_at_least(self, minimum: str) -> bool:
        order = ["info", "warning", "critical", "emergency"]
        return order.index(self.severity) >= order.index(minimum)


class AnomalySignal(BaseModel):
    """Provider-agnostic symptom trigger payload."""

    signal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: str = Field(
        default="prometheus",
        description="Source of alert: prometheus, cloudwatch, otel, ppa_forecaster",
    )
    provider: str = Field(
        default="kubernetes",
        description="Infrastructure provider: aws, kubernetes, hybrid",
    )
    environment: str = Field(
        default="production",
        description="Environment: dev, staging, production, production_tier_0",
    )
    target_service: str = Field(..., description="Name of affected service")
    target_resource_id: str = Field(
        default="",
        description="Identifier of resource (e.g. arn, pod name, deployment)",
    )
    symptom_summary: str = Field(
        ..., description="Short plain-English summary of observed symptom"
    )
    raw_metrics_or_alert: dict[str, Any] = Field(
        default_factory=dict, description="Alert payload or metrics snapshot"
    )
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

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
        """Returns the canonical NATS subject for this anomaly signal."""
        return f"nexus.anomaly.{self.provider}.{self.target_service}"

    def to_nats_payload(self) -> bytes:
        """Serializes the anomaly signal for NATS publish."""
        return self.model_dump_json().encode("utf-8")

    @classmethod
    def from_nats_payload(cls, payload: bytes) -> AnomalySignal:
        """Deserializes an anomaly signal received from NATS."""
        return cls.model_validate_json(payload)
