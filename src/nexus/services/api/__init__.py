# nexus.services.api — Phase 7 Observability Layer
# ====================================================
# Prometheus metrics + FastAPI status/control API

from nexus.services.api.status import status_api
from nexus.services.api.status.status_api import NexusContext, app, context
from nexus.services.observability.metrics.metrics import NexusMetrics, get_metrics

__all__ = [
    "NexusMetrics",
    "get_metrics",
    "app",
    "context",
    "NexusContext",
    "status_api",
]
