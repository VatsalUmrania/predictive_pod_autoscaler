"""
NEXUS Incident Event Schema
============================
The normalized lingua franca for all agent-to-orchestrator communication.

Every domain agent transforms its raw observation into an IncidentEvent
before publishing to NATS JetStream. This strict schema ensures:
  - Every downstream consumer speaks the same language
  - Correlation across agents is deterministic (shared fields)
  - The Orchestrator can reason about events without knowing agent internals
  - The Audit Trail has consistent, queryable records

NATS subject convention:
    nexus.incidents.<AgentType>.<SignalType>
    e.g. nexus.incidents.k8s.pod_crashloop
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


# ──────────────────────────────────────────────────────────────────────────────
# Enums
# ──────────────────────────────────────────────────────────────────────────────

class AgentType(str, Enum):
    NGINX       = "nginx"
    GIT         = "git"
    METRICS     = "metrics"
    DB          = "db"
    NETWORK     = "network"
    CONFIG      = "config"
    K8S         = "k8s"
    ORCHESTRATOR = "orchestrator"  # for system-level internal events


class SignalType(str, Enum):
    # ── MetricsAgent ──────────────────────────────────────────────────────────
    ANOMALY_DETECTED        = "anomaly_detected"
    THRESHOLD_BREACH        = "threshold_breach"
    METRIC_UNAVAILABLE      = "metric_unavailable"
    CIRCUIT_BREAKER_TRIPPED = "circuit_breaker_tripped"

    # ── GitAgent ───────────────────────────────────────────────────────────────
    DEPLOY_EVENT            = "deploy_event"
    DEPLOY_BLOCKED          = "deploy_blocked"
    ENV_CONTRACT_VIOLATION  = "env_contract_violation"
    SECRET_COMMITTED        = "secret_committed"
    ROLLBACK_SUGGESTED      = "rollback_suggested"

    # ── K8sAgent ──────────────────────────────────────────────────────────────
    POD_CRASHLOOP           = "pod_crashloop"
    POD_OOMKILLED           = "pod_oomkilled"
    POD_PENDING             = "pod_pending"
    DEPLOYMENT_DEGRADED     = "deployment_degraded"
    ROLLOUT_STUCK           = "rollout_stuck"
    HPA_MAXED               = "hpa_maxed"

    # ── NginxAgent ────────────────────────────────────────────────────────────
    HIGH_ERROR_RATE         = "high_error_rate"
    HIGH_LATENCY            = "high_latency"
    UPSTREAM_DOWN           = "upstream_down"
    TRAFFIC_SPIKE           = "traffic_spike"

    # ── DBAgent ───────────────────────────────────────────────────────────────
    DB_CONNECTION_EXHAUSTION = "db_connection_exhaustion"
    SLOW_QUERY_DETECTED     = "slow_query_detected"
    DB_QUERY_SPIKE          = "db_query_spike"
    DB_LOCK_CONTENTION      = "db_lock_contention"
    DB_REPLICATION_LAG      = "db_replication_lag"

    # ── NetworkAgent ──────────────────────────────────────────────────────────
    DNS_RESOLUTION_FAILURE  = "dns_resolution_failure"
    SERVICE_UNREACHABLE     = "service_unreachable"
    NETWORK_PARTITION       = "network_partition"
    INTER_SERVICE_LATENCY   = "inter_service_latency_high"

    # ── ConfigAgent ───────────────────────────────────────────────────────────
    CONFIG_DRIFT            = "config_drift"
    ENV_KEY_MISSING         = "env_key_missing"
    SECRET_MISMATCH         = "secret_mismatch"
    IAC_DRIFT               = "iac_drift"

    # ── Predictive Layer ──────────────────────────────────────────────────────
    TRAFFIC_SPIKE_PREDICTED = "traffic_spike_predicted"
    ANOMALY_PREDICTED       = "anomaly_predicted"


class Severity(str, Enum):
    INFO      = "info"
    WARNING   = "warning"
    CRITICAL  = "critical"
    EMERGENCY = "emergency"


class HealingLevel(int, Enum):
    """Maps to the 4-level Healing Actions Ladder in the Governance Plane."""
    L0_ALERT_ONLY        = 0  # Detect + alert; no infra changes
    L1_NO_REGRET         = 1  # Pod restart, flush cache, scale +1 — always safe
    L2_BOUNDED_MITIGATION = 2  # Canary shift, rate-limit, drain — bounded blast radius
    L3_HIGH_RISK         = 3  # Rollback, config patch, DNS mutation — human approval if confidence < 0.85


# ──────────────────────────────────────────────────────────────────────────────
# Core Event
# ──────────────────────────────────────────────────────────────────────────────

class IncidentEvent(BaseModel):
    """
    The normalized NEXUS incident event.

    All domain agents must emit this structure to NATS before any downstream
    component (Runbook Executor, Orchestrator, Knowledge Base) can act on it.

    Fields:
        event_id          — UUID, auto-generated
        timestamp         — UTC datetime of observation, auto-generated
        agent             — Which agent produced this event
        signal_type       — What was observed
        severity          — INFO / WARNING / CRITICAL / EMERGENCY
        namespace         — K8s namespace (if applicable)
        resource_name     — Deployment / Pod / Service name (if applicable)
        resource_kind     — K8s resource kind (if applicable)
        correlation_id    — Groups related events into one logical incident
        deploy_sha        — Git SHA of the most recent deploy (for correlation)
        parent_event_id   — Links follow-up events to their parent
        context           — Agent-specific structured payload (see typed helpers)
        raw               — Optional full raw observation (omit in high-volume streams)
        suggested_runbook — Optional runbook ID the agent recommends
        suggested_healing_level — Optional healing level hint from the agent
        confidence        — 0.0–1.0, agent's confidence in its own diagnosis
    """

    # Identity
    event_id:   str      = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp:  datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Source
    agent:       AgentType
    signal_type: SignalType
    severity:    Severity

    # Resource targeting
    namespace:     Optional[str] = None
    resource_name: Optional[str] = None   # e.g. "payments-api"
    resource_kind: Optional[str] = None   # e.g. "Deployment"

    # Correlation
    correlation_id:   Optional[str] = None  # Groups related events into one incident
    deploy_sha:       Optional[str] = None  # Ties event to a deployment
    parent_event_id:  Optional[str] = None  # For child / follow-up events

    # Payload
    context: Dict[str, Any] = Field(default_factory=dict)
    """
    Agent-specific structured context. Use the typed context helpers below
    to build this dict (e.g. MetricsAnomalyContext(...).model_dump()).
    """

    raw: Optional[Dict[str, Any]] = None
    """Full raw observation. Omit in high-volume streams to save bandwidth."""

    # Advisory fields (agent can optionally suggest a path)
    suggested_runbook:       Optional[str]          = None
    suggested_healing_level: Optional[HealingLevel] = None
    confidence:              Optional[float]         = Field(None, ge=0.0, le=1.0)

    model_config = {"use_enum_values": True}

    @field_validator("timestamp", mode="before")
    @classmethod
    def ensure_utc(cls, v: Any) -> datetime:
        if isinstance(v, str):
            v = datetime.fromisoformat(v)
        if isinstance(v, datetime) and v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v

    # ── Convenience helpers ───────────────────────────────────────────────────

    def nats_subject(self) -> str:
        """Returns the canonical NATS subject for this event."""
        return f"nexus.incidents.{self.agent}.{self.signal_type}"

    def to_nats_payload(self) -> bytes:
        """Serializes the event for NATS publish."""
        return self.model_dump_json().encode("utf-8")

    @classmethod
    def from_nats_payload(cls, payload: bytes) -> "IncidentEvent":
        """Deserializes an event received from NATS."""
        return cls.model_validate_json(payload)

    def is_severity_at_least(self, minimum: str) -> bool:
        order = ["info", "warning", "critical", "emergency"]
        return order.index(self.severity) >= order.index(minimum)


# ──────────────────────────────────────────────────────────────────────────────
# Typed context helpers (use .model_dump() to populate IncidentEvent.context)
# These are not enforced — they serve as documentation + IDE support
# ──────────────────────────────────────────────────────────────────────────────

class MetricsAnomalyContext(BaseModel):
    metric_name:       str
    current_value:     float
    threshold:         float
    anomaly_score:     float               # Output of GRU Autoencoder
    feature_vector:    Optional[List[float]] = None
    window_seconds:    int = 60


class DeployEventContext(BaseModel):
    sha:              str
    branch:           str
    author:           str
    deployment_name:  str
    namespace:        str
    previous_sha:     Optional[str] = None
    changed_files:    List[str] = Field(default_factory=list)


class EnvContractViolationContext(BaseModel):
    missing_keys:     List[str]
    present_keys:     List[str]
    deployment_name:  str
    namespace:        str
    source_file:      Optional[str] = None  # Which source file referenced the key


class PodFailureContext(BaseModel):
    pod_name:         str
    deployment_name:  str
    restart_count:    int
    exit_code:        Optional[int] = None
    reason:           str            # OOMKilled | Error | CrashLoopBackOff
    memory_usage_mi:  Optional[float] = None
    memory_limit_mi:  Optional[float] = None
    node_name:        Optional[str] = None


class NginxHighErrorContext(BaseModel):
    endpoint:         str
    error_rate:       float          # 0.0 – 1.0
    baseline_rate:    float
    rps:              float
    window_seconds:   int = 60
    upstream:         Optional[str] = None
    status_codes:     Dict[str, int] = Field(default_factory=dict)  # {"500": 42, "503": 7}


class DBConnectionExhaustionContext(BaseModel):
    db_engine:           str          # postgres | mysql | mongodb
    db_host:             str
    active_connections:  int
    max_connections:     int
    utilization_pct:     float
    blocking_query:      Optional[str] = None
    top_waiting_queries: List[str] = Field(default_factory=list)


class DNSFailureContext(BaseModel):
    hostname:                    str
    resolvers_tried:             List[str]
    error_message:               str
    last_successful_resolution:  Optional[datetime] = None
    affected_services:           List[str] = Field(default_factory=list)


class ConfigDriftContext(BaseModel):
    resource_kind:        str
    resource_name:        str
    namespace:            str
    drift_fields:         List[str]
    expected_hash:        str
    actual_hash:          str
    drift_severity_score: float   # 0.0 – 1.0
    manual_change_author: Optional[str] = None


class TrafficSpikePredictionContext(BaseModel):
    endpoint:                   str
    predicted_rps:              float
    current_rps:                float
    prediction_horizon_minutes: int
    db_table_trigger:           Optional[str] = None  # Which DB table drove this prediction
    model_smape:                Optional[float] = None
    confidence:                 float = 0.0
