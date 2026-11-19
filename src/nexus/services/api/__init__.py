# nexus.services.api — Phase 7 Observability Layer
# ====================================================
# Prometheus metrics + FastAPI status/control API

from nexus.services.api.status_api import NexusContext, app, context
from nexus.services.observability.metrics import NexusMetrics, get_metrics

__all__ = [
    "NexusMetrics",
    "get_metrics",
    "app",
    "context",
    "NexusContext",
]
