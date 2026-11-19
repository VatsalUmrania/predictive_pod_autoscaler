"""
NATS publisher and timestamp utilities for SDK Ingest.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_sdk_nats_nc = None
_sdk_nats_lock = None

_TYPE_MAP = {
    # Standard SDK events
    "route_error": "HIGH_ERROR_RATE",
    "js_error": "HIGH_ERROR_RATE",
    "unhandled_rejection": "HIGH_ERROR_RATE",
    "failed_fetch": "HIGH_ERROR_RATE",
    "failed_xhr": "HIGH_ERROR_RATE",
    "db_query": "SLOW_QUERY_DETECTED",
    "function_error": "HIGH_ERROR_RATE",
    "web_vital": "THRESHOLD_BREACH",
    # Demo injection events
    "db_spike_injected": "SLOW_QUERY_DETECTED",
    "error_rate_injected": "HIGH_ERROR_RATE",
    "slow_query_injected": "SLOW_QUERY_DETECTED",
    "oom_injected": "THRESHOLD_BREACH",
    "dns_error_injected": "HIGH_ERROR_RATE",
    "config_drift_detected": "ENV_CONTRACT_VIOLATION",
    "bad_config": "ENV_CONTRACT_VIOLATION",
    "env_contract_violation": "ENV_CONTRACT_VIOLATION",
    "secret_committed": "ENV_CONTRACT_VIOLATION",
    "code_quality_warning": "HIGH_ERROR_RATE",
}

_IGNORE = {"heartbeat", "deploy_event", "healing_completed"}


def ts_to_iso(ts: float | None) -> str:
    """Convert millisecond timestamp to ISO8601 string."""
    if ts is None:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()


async def get_nats():
    """Return (or lazily create) a raw NATS core connection for SDK publishing."""
    global _sdk_nats_nc, _sdk_nats_lock
    if _sdk_nats_lock is None:
        _sdk_nats_lock = asyncio.Lock()
    async with _sdk_nats_lock:
        if _sdk_nats_nc is None or _sdk_nats_nc.is_closed:
            try:
                import nats as _nats_lib

                _url = os.environ.get("NATS_URL", "nats://localhost:4222")
                _sdk_nats_nc = await _nats_lib.connect(
                    _url,
                    name="nexus-sdk-ingest",
                    max_reconnect_attempts=5,
                )
            except Exception as _e:
                logger.warning(f"[SDKIngest] NATS connection failed: {_e}")
                _sdk_nats_nc = None
        return _sdk_nats_nc


async def publish_sdk_to_nats(
    app_name: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Translate an SDK payload into an IncidentEvent and publish to NATS."""
    if event_type in _IGNORE:
        return

    try:
        from nexus.core.bus.events import (
            AgentType,
            IncidentEvent,
            Severity,
            SignalType,
        )

        signal_name = _TYPE_MAP.get(event_type)
        if signal_name is None:
            return

        signal = getattr(SignalType, signal_name)
        event = IncidentEvent(
            agent=AgentType.ORCHESTRATOR,
            signal_type=signal,
            severity=Severity.WARNING,
            namespace="sdk",
            resource_name=app_name,
            resource_kind="Application",
            context={"sdk_event_type": event_type, "app": app_name, **payload},
        )

        nc = await get_nats()
        if nc and not nc.is_closed:
            subject = f"nexus.incidents.sdk.{event_type}"
            await nc.publish(subject, event.model_dump_json().encode())
            logger.info(f"[SDKIngest] → NATS {subject}")
        else:
            logger.warning(f"[SDKIngest] NATS unavailable — {event_type} dropped")
    except Exception as exc:
        logger.warning(f"[SDKIngest] publish error: {exc}")
