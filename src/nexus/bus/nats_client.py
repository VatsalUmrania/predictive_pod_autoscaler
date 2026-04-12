"""
NEXUS NATS JetStream Client
============================
Async publish/subscribe abstraction over NATS JetStream.

Design decisions:
  - Single stream (NEXUS_INCIDENTS) with wildcard subject binding
  - JetStream for at-least-once delivery + replay on consumer lag
  - Typed publish: callers pass IncidentEvent, not raw bytes
  - Subscriber receives fully-deserialized IncidentEvent in handler
  - Connection managed as async context manager for clean teardown

Usage (publisher):
    async with NATSClient() as nc:
        await nc.publish(event)

Usage (subscriber):
    async with NATSClient() as nc:
        await nc.subscribe(agent_filter="k8s.*", handler=my_handler)
        await asyncio.sleep(float("inf"))  # keep alive

Handler signature:
    async def my_handler(event: IncidentEvent) -> None: ...
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, List, Optional

import nats
from nats.aio.client import Client as NATSConnection
from nats.js import JetStreamContext
from nats.js.api import StreamConfig, RetentionPolicy, StorageType

from nexus.bus.incident_event import IncidentEvent

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

NEXUS_STREAM    = "NEXUS_INCIDENTS"
NEXUS_SUBJECT   = "nexus.incidents"          # Base subject prefix
NEXUS_WILDCARD  = f"{NEXUS_SUBJECT}.>"      # Matches all agent/signal combos

# JetStream stream config: retain 24h of messages, max 50MB
_STREAM_CONFIG = StreamConfig(
    name=NEXUS_STREAM,
    subjects=[NEXUS_WILDCARD],
    retention=RetentionPolicy.LIMITS,
    storage=StorageType.MEMORY,     # Use FILE in production
    max_age=86_400,                  # 24 hours in seconds
    max_bytes=50 * 1024 * 1024,     # 50 MB
    duplicate_window=60,            # De-dupe window: 60s
)

HandlerType = Callable[[IncidentEvent], Awaitable[None]]


# ──────────────────────────────────────────────────────────────────────────────
# Client
# ──────────────────────────────────────────────────────────────────────────────

class NATSClient:
    """
    Async NATS JetStream client for the NEXUS incident event bus.

    Handles connection lifecycle, stream creation idempotency,
    typed publish, and subject-filtered subscriptions.
    """

    def __init__(
        self,
        nats_url: str = "nats://localhost:4222",
        connect_timeout: float = 10.0,
        reconnect_attempts: int = 60,
    ):
        self._url               = nats_url
        self._connect_timeout   = connect_timeout
        self._reconnect_attempts = reconnect_attempts
        self._nc: Optional[NATSConnection] = None
        self._js: Optional[JetStreamContext] = None
        self._subscribers: List[asyncio.Task] = []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Connect to NATS and ensure the JetStream stream exists."""
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
        await self._ensure_stream()
        logger.info("[NATS] Connected and stream ready")

    async def close(self) -> None:
        """Drain subscriptions and close connection cleanly."""
        for task in self._subscribers:
            task.cancel()
        if self._nc and not self._nc.is_closed:
            await self._nc.drain()
        logger.info("[NATS] Connection closed")

    async def __aenter__(self) -> "NATSClient":
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    # ── Stream management ─────────────────────────────────────────────────────

    async def _ensure_stream(self) -> None:
        """Create the NEXUS_INCIDENTS stream if it does not already exist."""
        try:
            await self._js.find_stream(NEXUS_WILDCARD)
            logger.debug(f"[NATS] Stream '{NEXUS_STREAM}' already exists")
        except Exception:
            try:
                await self._js.add_stream(_STREAM_CONFIG)
                logger.info(f"[NATS] Created stream '{NEXUS_STREAM}'")
            except Exception as exc:
                # Stream may have been created by a concurrent instance — ignore
                logger.warning(f"[NATS] Stream creation warning (likely harmless): {exc}")

    # ── Publish ───────────────────────────────────────────────────────────────

    async def publish(self, event: IncidentEvent) -> None:
        """
        Publish an IncidentEvent to the NATS JetStream bus.

        Subject: nexus.incidents.<agent>.<signal_type>
        Payload: JSON-encoded IncidentEvent
        """
        if not self._js:
            raise RuntimeError("NATSClient not connected. Call connect() first.")

        subject = event.nats_subject()
        payload = event.to_nats_payload()

        ack = await self._js.publish(
            subject,
            payload,
            headers={"Nats-Msg-Id": event.event_id},  # JetStream de-dupe key
        )
        logger.debug(f"[NATS] Published {subject} seq={ack.seq} event_id={event.event_id}")

    # Alias for backwards compat with plan references
    async def publish_incident(self, event: IncidentEvent) -> None:
        await self.publish(event)

    # ── Subscribe ─────────────────────────────────────────────────────────────

    async def subscribe(
        self,
        handler: HandlerType,
        agent_filter: str = ">",
        signal_filter: str = ">",
        durable_name: Optional[str] = None,
        queue_group: Optional[str] = None,
    ) -> None:
        """
        Subscribe to incident events matching the given filters.

        Args:
            handler:       Async callback receiving a deserialized IncidentEvent.
            agent_filter:  NATS wildcard for the agent segment. ">" = all agents.
                           Example: "k8s" | "metrics" | "k8s.>"
            signal_filter: NATS wildcard for the signal segment. ">" = all signals.
            durable_name:  JetStream durable consumer name (for persistent offset tracking).
            queue_group:   Queue group for load-balanced consumers.

        Subject pattern: nexus.incidents.<agent_filter>.<signal_filter>
        """
        if not self._js:
            raise RuntimeError("NATSClient not connected.")

        subject = f"{NEXUS_SUBJECT}.{agent_filter}"
        if signal_filter != ">":
            subject = f"{NEXUS_SUBJECT}.{agent_filter}.{signal_filter}"

        logger.info(f"[NATS] Subscribing to '{subject}' durable={durable_name}")

        async def _raw_handler(msg):
            try:
                event = IncidentEvent.from_nats_payload(msg.data)
                await handler(event)
                await msg.ack()
            except Exception as exc:
                logger.error(f"[NATS] Handler error on subject '{msg.subject}': {exc}", exc_info=True)
                await msg.nak()

        sub_kwargs = {"stream": NEXUS_STREAM}
        if durable_name:
            sub_kwargs["durable"] = durable_name
        if queue_group:
            sub_kwargs["queue"] = queue_group

        sub = await self._js.subscribe(subject, cb=_raw_handler, **sub_kwargs)

        # Keep subscription task alive
        task = asyncio.create_task(self._keep_alive(sub))
        self._subscribers.append(task)

    @staticmethod
    async def _keep_alive(sub) -> None:
        """Keeps the subscription task alive indefinitely."""
        try:
            while True:
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            await sub.unsubscribe()

    # ── Error callbacks ───────────────────────────────────────────────────────

    @staticmethod
    async def _on_error(exc: Exception) -> None:
        logger.error(f"[NATS] Error: {exc}")

    @staticmethod
    async def _on_disconnect() -> None:
        logger.warning("[NATS] Disconnected from server")

    @staticmethod
    async def _on_reconnect() -> None:
        logger.info("[NATS] Reconnected to server")

    # ── Health ────────────────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._nc is not None and self._nc.is_connected
