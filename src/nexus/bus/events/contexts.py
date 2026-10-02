"""Typed context helpers for IncidentEvent payloads."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class MetricsAnomalyContext(BaseModel):
    metric_name: str
    current_value: float
    threshold: float
    anomaly_score: float  # Output of GRU Autoencoder
    feature_vector: list[float] | None = None
    window_seconds: int = 60


class DeployEventContext(BaseModel):
    sha: str
    branch: str
    author: str
    deployment_name: str
    namespace: str
    previous_sha: str | None = None
    changed_files: list[str] = Field(default_factory=list)


class EnvContractViolationContext(BaseModel):
    missing_keys: list[str]
    present_keys: list[str]
    deployment_name: str
    namespace: str
    source_file: str | None = None  # Which source file referenced the key


class PodFailureContext(BaseModel):
    pod_name: str
    deployment_name: str
    restart_count: int
    exit_code: int | None = None
    reason: str  # OOMKilled | Error | CrashLoopBackOff
    memory_usage_mi: float | None = None
    memory_limit_mi: float | None = None
    node_name: str | None = None


class NginxHighErrorContext(BaseModel):
    endpoint: str
    error_rate: float  # 0.0 – 1.0
    baseline_rate: float
    rps: float
    window_seconds: int = 60
    upstream: str | None = None
    status_codes: dict[str, int] = Field(default_factory=dict)  # {"500": 42, "503": 7}


class DBConnectionExhaustionContext(BaseModel):
    db_engine: str  # postgres | mysql | mongodb
    db_host: str
    active_connections: int
    max_connections: int
    utilization_pct: float
    blocking_query: str | None = None
    top_waiting_queries: list[str] = Field(default_factory=list)


class DNSFailureContext(BaseModel):
    hostname: str
    resolvers_tried: list[str]
    error_message: str
    last_successful_resolution: datetime | None = None
    affected_services: list[str] = Field(default_factory=list)


class ConfigDriftContext(BaseModel):
    resource_kind: str
    resource_name: str
    namespace: str
    drift_fields: list[str]
    expected_hash: str
    actual_hash: str
    drift_severity_score: float  # 0.0 – 1.0
    manual_change_author: str | None = None


class TrafficSpikePredictionContext(BaseModel):
    endpoint: str
    predicted_rps: float
    current_rps: float
    prediction_horizon_minutes: int
    db_table_trigger: str | None = None  # Which DB table drove this prediction
    model_smape: float | None = None
    confidence: float = 0.0
