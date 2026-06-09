"""PPA NATS JetStream client for observer-mode prediction streaming.

Publishes:
    ppa.predictions.{deployment}  — LSTM prediction events (30s cadence)
    ppa.outcomes.{deployment}    — Prediction outcome verdicts

Design:
    - Separate stream from NEXUS_INCIDENTS to isolate concerns
    - Raw dict payloads for simpler PPA-side code
    - Lazy connect; accumulate pending events in memory on failure
    - Never blocks kopf reconciliation on NATS IO
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import nats
from nats.js.api import RetentionPolicy, StorageType, StreamConfig

if TYPE_CHECKING:
    from nats.aio.client import Client as NATSConnection

logger = logging.getLogger(__name__)

PPA_PREDICTION_STREAM = "PPA_PREDICTIONS"


class PpaNATSClient:
    def __init__(
        self,
        nats_url: str = "nats://localhost:4222",
        connect_timeout: float = 10.0,
        reconnect_attempts: int = 10,
    ):
        self._url = nats_url
        self._connect_timeout = connect_timeout
        self._reconnect_attempts = reconnect_attempts
        self._nc: NATSConnection | None = None  # type: ignore[assignment]
        self._js: Any = None

    async def _ensure_stream(self) -> None:
        js = self._js
        if js is None:
            return
        cfg = StreamConfig(
            name=PPA_PREDICTION_STREAM,
            subjects=["ppa.predictions.>", "ppa.outcomes.>"],
            retention=RetentionPolicy.LIMITS,
            storage=StorageType.MEMORY,
            max_age=3600,
            max_bytes=10 * 1024 * 1024,
        )
        try:
            await js.add_stream(cfg)  # type: ignore[attr-defined]
            logger.info(f"[PPA-NATS] Created stream '{PPA_PREDICTION_STREAM}'")
        except Exception as exc:
            # err_code=10058 → stream already exists with a different config.
            # Reconcile by updating to the desired config instead of warning.
            exc_str = str(exc)
            if "10058" in exc_str or "already in use" in exc_str:
                try:
                    await js.update_stream(cfg)  # type: ignore[attr-defined]
                    logger.info(f"[PPA-NATS] Updated stream '{PPA_PREDICTION_STREAM}' config")
                except Exception as upd_exc:
                    logger.warning(f"[PPA-NATS] Stream update failed (non-fatal): {upd_exc}")
            else:
                logger.warning(f"[PPA-NATS] Stream ensure failed (non-fatal): {exc}")

    async def connect(self) -> None:
        self._nc = await nats.connect(
            self._url,
            connect_timeout=self._connect_timeout,
            max_reconnect_attempts=self._reconnect_attempts,
            reconnect_time_wait=2,
        )
        self._js = self._nc.jetstream()
        await self._ensure_stream()
        logger.info("[PPA-NATS] Connected to %s", self._url)

    async def publish(self, subject: str, payload: dict) -> None:
        if not self._js:
            raise RuntimeError(
                "PpaNATSClient not connected. Call connect() first."
            )
        data = json.dumps(payload).encode("utf-8")
        ack = await self._js.publish(subject, data, stream=PPA_PREDICTION_STREAM)
        logger.debug(f"[PPA-NATS] Published {subject} seq={ack.seq}")

    async def close(self) -> None:
        if self._nc and not self._nc.is_closed:
            await self._nc.drain()
        logger.info("[PPA-NATS] Connection closed")

    async def __aenter__(self) -> PpaNATSClient:
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    @property
    def is_connected(self) -> bool:
        return self._nc is not None and self._nc.is_connected
