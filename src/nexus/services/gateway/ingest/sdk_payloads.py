"""
Pydantic payload models for NEXUS SDK Ingest.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    app_name: str = Field(..., min_length=1)
    tier: str = Field("production")
    contact: str | None = None


class GenericEventPayload(BaseModel):
    type: str
    message: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    ts: float | None = None


class HeartbeatPayload(BaseModel):
    memory_mb: float | None = None
    cpu_pct: float | None = None
    uptime_s: float | None = None
    error_rate: float | None = None
    rps: float | None = None
    version: str | None = None
    ts: float | None = None


class RouteErrorPayload(BaseModel):
    route: str
    method: str = "GET"
    status_code: int
    duration_ms: float | None = None
    error_msg: str | None = None
    ts: float | None = None


class QueryPayload(BaseModel):
    sql_preview: str
    duration_ms: float
    tables: list[str] = Field(default_factory=list)
    slow: bool = False
    ts: float | None = None


class FrontendPayload(BaseModel):
    type: str
    message: str | None = None
    stack: str | None = None
    source: str | None = None
    url: str | None = None
    name: str | None = None
    value: float | None = None
    status: int | None = None
    ts: float | None = None
