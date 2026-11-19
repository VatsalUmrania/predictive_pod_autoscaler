"""
NEXUS Status API Context Container and Lifecycle Management
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException

logger = logging.getLogger(__name__)

_routers_included = False
_ppa_outcome_tracker = None
_nats_client: Any = None


def _include_integration_routers(app: FastAPI) -> None:
    global _routers_included
    if _routers_included:
        return
    try:
        from nexus.services.gateway.ingest.sdk_ingest import router as sdk_router
        from nexus.services.observability.dashboard.dashboard import (
            router as dev_router,
        )

        if not any(
            getattr(r, "path", None) == "/developer/incidents" for r in app.routes
        ):
            app.include_router(sdk_router)
            app.include_router(dev_router)
        _routers_included = True
    except ImportError as exc:
        logger.warning(f"[StatusAPI] Integration routers not loaded: {exc}")


@dataclass
class NexusContext:
    """Dependency injection container holding all NEXUS runtime singletons."""

    workflow: Any = None
    orchestrator: Any = None
    prescaler: Any = None
    feedback_loop: Any = None
    ppa_outcome_tracker: Any = None
    outcome_store: Any = None
    knowledge_base: Any = None
    audit_trail: Any = None
    db_client: Any = None
    policy_engine: Any = None
    action_ladder: Any = None
    started_at: float = field(default_factory=time.monotonic)

    def uptime_seconds(self) -> float:
        return round(time.monotonic() - self.started_at, 1)


context = NexusContext()


def _require(component: Any, name: str) -> Any:
    if component is None:
        raise HTTPException(status_code=503, detail=f"{name} not configured")
    return component


async def _on_ppa_prediction(data: dict, subject: str) -> None:
    """Handle incoming ppa.predictions.* events — delegate to OutcomeTracker."""
    if _ppa_outcome_tracker is None:
        return
    try:
        subject_parts = subject.split(".")
        deployment = subject_parts[-1] if len(subject_parts) > 2 else "unknown"
        await _ppa_outcome_tracker.on_prediction(
            deployment=deployment,
            namespace=data.get("namespace", "default"),
            predicted_rps=float(data.get("predicted_rps", 0)),
            current_rps=float(data.get("current_rps", 0)),
            confidence=float(data.get("confidence", 0)),
            horizon_minutes=int(data.get("horizon_minutes", 10)),
            model_version=data.get("model_version", "unknown"),
            raw_features=data.get("raw_features", {}),
            event_timestamp=data.get("timestamp", ""),
        )
    except Exception as exc:
        logger.warning(f"[_on_ppa_prediction] {exc}")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Initialize async resources on startup, or skip if already pre-populated."""
    global _ppa_outcome_tracker, _nats_client

    if context.orchestrator is not None or context.workflow is not None:
        logger.info("[StatusAPI] NexusContext pre-populated — skipping self-init")
        _ppa_outcome_tracker = context.ppa_outcome_tracker
        _include_integration_routers(app)
        yield
        return

    try:
        from nexus.core.database.connection.client import get_database_client

        context.db_client = await get_database_client()
        logger.info("[StatusAPI] PostgreSQL client connected")
    except Exception as exc:
        logger.warning(f"[StatusAPI] Database init skipped/failed ({exc})")

    try:
        from nexus.services.gateway.auth.token_store import get_token_store

        store = get_token_store()
        await store.init()
    except Exception as exc:
        logger.warning(f"[StatusAPI] TokenStore init skipped: {exc}")

    try:
        from nexus.core.bus.nats.client import NATSClient

        _nats_url = os.environ.get("NATS_URL", "nats://localhost:4222")
        _nats_client = NATSClient(nats_url=_nats_url, reconnect_attempts=10)
        await _nats_client.connect()
        logger.info(f"[StatusAPI] NATS connected: {_nats_url}")
    except Exception as exc:
        logger.warning(f"[StatusAPI] NATS connection failed ({exc})")
        _nats_client = None

    _ppa_outcome_tracker = None
    if _nats_client:
        try:
            from nexus.intelligence.learning.ppa.ppa_outcome_tracker import (
                PpaOutcomeTracker,
            )

            _ppa_outcome_tracker = PpaOutcomeTracker(nats_client=_nats_client)
            await _ppa_outcome_tracker.start()
            await _nats_client.subscribe_raw(
                "ppa.predictions.>",
                handler=_on_ppa_prediction,
                stream_name="PPA_PREDICTIONS",
            )
            logger.info("[StatusAPI] PPA OutcomeTracker started")

            try:
                from nexus.intelligence.predictive.scaling.prescaler import Prescaler

                context.prescaler = Prescaler(nats_client=_nats_client)
                await context.prescaler.subscribe_to_ppa_predictions()
                logger.info("[StatusAPI] Prescaler subscribed to ppa.predictions.*")
            except Exception as exc:
                logger.warning(f"[StatusAPI] Prescaler init failed ({exc})")
        except Exception as exc:
            logger.warning(f"[StatusAPI] PPA OutcomeTracker init failed ({exc})")

    _include_integration_routers(app)
    yield

    if _ppa_outcome_tracker:
        try:
            await _ppa_outcome_tracker.stop()
        except Exception:
            pass
    if _nats_client:
        try:
            await _nats_client.close()
        except Exception:
            pass
