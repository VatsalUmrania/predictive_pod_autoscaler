"""NEXUS NATS JetStream Client."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import nats
from nats.aio.client import Client as NATSConnection
from nats.js import JetStreamContext
from nats.js.api import DeliverPolicy

from nexus.core.bus.events.event import IncidentEvent
from nexus.core.bus.nats.constants import (
    _PPA_STREAM_CONFIG,
    _STREAM_CONFIG,
    NEXUS_STREAM,
    PPA_STREAM,
)
from nexus.core.bus.nats.stream_manager import ensure_stream
from nexus.core.bus.nats.subscriber import (
    HandlerType,
    RawHandlerType,
    create_incident_subscription,
    create_raw_subscription,
)

logger = logging.getLogger(__name__)


class NATSClient:
    """Async NATS JetStream client for the NEXUS incident event bus."""

    _instance: NATSClient | None = None

    def __init__(
        self,
        nats_url: str = "nats://localhost:4222",
        connect_timeout: float = 10.0,
        reconnect_attempts: int = 60,
    ):
        self._url = nats_url
        self._connect_timeout = connect_timeout
        self._reconnect_attempts = reconnect_attempts
        self._nc: NATSConnection | None = None
        self._js: JetStreamContext | None = None
        self._subscribers: list[asyncio.Task] = []
        NATSClient._instance = self

    @classmethod
    def get_instance(cls) -> NATSClient:
        """Return the active singleton instance or create a default instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def connect(self) -> None:
        """Connect to NATS and ensure the JetStream streams exist."""
        logger.info(f"[NATS] Connecting to {self._url}")
        self._nc = await nats.connect(
            self._url,
            connect_timeout=self._connect_timeout,
            max_reconnect_attempts=self._reconnect_attempts,
            reconnect_time_wait=2,
            error_cb=self._on_error,
            disconnected_cb=self._on_disconnect,
            reconnected_cb=self._on_reconnect,
        )
        self._js = self._nc.jetstream()
        await ensure_stream(self._js, _STREAM_CONFIG, NEXUS_STREAM)
        await ensure_stream(self._js, _PPA_STREAM_CONFIG, PPA_STREAM)
        logger.info("[NATS] Connected and streams ready")

    async def close(self) -> None:
        """Drain subscriptions and close connection cleanly."""
        for task in self._subscribers:
            task.cancel()
        if self._nc and not self._nc.is_closed:
            await self._nc.drain()
        logger.info("[NATS] Connection closed")

    async def __aenter__(self) -> NATSClient:
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    async def publish(
        self,
        event_or_subject: Any = None,
        payload: Any = None,
        headers: dict[str, str] | None = None,
        subject: str | None = None,
    ) -> None:
        """Publish an IncidentEvent or raw payload to the NATS JetStream bus."""
        if not self._js:
            raise RuntimeError("NATSClient not connected. Call connect() first.")

        target_subj = subject or (
            event_or_subject if isinstance(event_or_subject, str) else None
        )
        if target_subj:
            data = (
                payload
                if isinstance(payload, bytes)
                else (
                    json.dumps(payload).encode("utf-8")
                    if isinstance(payload, dict)
                    else str(payload).encode("utf-8")
                )
            )
            ack = await self._js.publish(target_subj, data, headers=headers)
            logger.debug(
                f"[NATS] Published {target_subj} seq={getattr(ack, 'seq', None)}"
            )
            return

        event = event_or_subject
        subj = event.nats_subject()
        body = event.to_nats_payload()
        hdr = headers or {"Nats-Msg-Id": getattr(event, "event_id", "msg")}
        ack = await self._js.publish(subj, body, headers=hdr)
        logger.debug(f"[NATS] Published {subj} seq={getattr(ack, 'seq', None)}")

    async def publish_incident(self, event: IncidentEvent) -> None:
        await self.publish(event)

    async def publish_raw(
        self, subject: str, payload: dict | bytes, headers: dict[str, str] | None = None
    ) -> None:
        """Publish a raw dict or bytes payload with optional headers to JetStream."""
        if not self._js:
            raise RuntimeError("NATSClient not connected. Call connect() first.")
        data = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload).encode("utf-8")
        )
        pub_kwargs: dict[str, Any] = {"stream": NEXUS_STREAM}
        if headers:
            pub_kwargs["headers"] = headers
        ack = await self._js.publish(subject, data, **pub_kwargs)
        logger.debug(f"[NATS] Published raw {subject} seq={getattr(ack, 'seq', None)}")

    async def subscribe(
        self,
        handler: HandlerType,
        agent_filter: str = ">",
        signal_filter: str = ">",
        durable_name: str | None = None,
        queue_group: str | None = None,
        deliver_policy: DeliverPolicy = DeliverPolicy.NEW,
    ) -> None:
        if not self._js:
            raise RuntimeError("NATSClient not connected.")
        task = await create_incident_subscription(
            self._js,
            handler,
            agent_filter,
            signal_filter,
            durable_name,
            queue_group,
            deliver_policy,
        )
        self._subscribers.append(task)

    async def subscribe_raw(
        self,
        subject_pattern: str,
        handler: RawHandlerType,
        durable_name: str | None = None,
        stream_name: str | None = None,
        deliver_policy: DeliverPolicy = DeliverPolicy.NEW,
    ) -> None:
        if not self._js:
            raise RuntimeError("NATSClient not connected.")
        task = await create_raw_subscription(
            self._js,
            subject_pattern,
            handler,
            durable_name,
            stream_name,
            deliver_policy,
        )
        self._subscribers.append(task)

    @staticmethod
    async def _on_error(exc: Exception) -> None:
        logger.error(f"[NATS] Error: {exc}")

    @staticmethod
    async def _on_disconnect() -> None:
        logger.warning("[NATS] Disconnected from server")

    @staticmethod
    async def _on_reconnect() -> None:
        logger.info("[NATS] Reconnected to server")

    @property
    def is_connected(self) -> bool:
        return self._nc is not None and self._nc.is_connected
