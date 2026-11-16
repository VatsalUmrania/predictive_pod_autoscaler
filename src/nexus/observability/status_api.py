"""
NEXUS Status API
=================
FastAPI application exposing health, system status, Prometheus metrics,
RCA decisions, prescaler mode, audit trail, approvals, and Slack interactivity.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from nexus.api.ingest import ingest_otlp_events
from nexus.api.ingest import router as ingest_router
from nexus.observability import status_context as _sc
from nexus.observability.status_context import (
    NexusContext,
    _include_integration_routers,
    _lifespan,
    _require,
    context,
)
from nexus.observability.status_governance import (
    approve_action,
    audit_by_incident,
    audit_tail,
    pending_approvals,
    reject_action,
)
from nexus.observability.status_governance import (
    router as governance_router,
)
from nexus.observability.status_routes import (
    health,
    last_rca,
    learning_status,
    prescaler_set_mode,
    prometheus_metrics,
    serve_dashboard,
    status,
)
from nexus.observability.status_routes import (
    router as core_router,
)
from nexus.observability.status_slack import (
    _slack_dispatch_approved,
    _slack_replace,
    _slack_replace_via_response_url,
    slack_interactive,
)
from nexus.observability.status_slack import (
    router as slack_router,
)

logger = logging.getLogger(__name__)

app = FastAPI(
    title="NEXUS Self-Healing Infrastructure",
    description="Status, control, and developer integration API for NEXUS",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=_lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(core_router)
app.include_router(governance_router)
app.include_router(slack_router)
app.include_router(ingest_router)
app.add_api_route("/v1/logs", ingest_otlp_events, methods=["POST"], tags=["ingest"])
_include_integration_routers(app)


def __getattr__(name: str) -> Any:
    if name in ("_ppa_outcome_tracker", "_nats_client"):
        return getattr(_sc, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "app",
    "context",
    "NexusContext",
    "_lifespan",
    "_require",
    "health",
    "status",
    "prometheus_metrics",
    "last_rca",
    "prescaler_set_mode",
    "learning_status",
    "serve_dashboard",
    "audit_tail",
    "audit_by_incident",
    "approve_action",
    "reject_action",
    "pending_approvals",
    "slack_interactive",
    "_slack_replace",
    "_slack_replace_via_response_url",
    "_slack_dispatch_approved",
]
