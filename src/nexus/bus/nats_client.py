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
from collections.abc import Awaitable, Callable

import nats
from nats.aio.client import Client as NATSConnection
from nats.js import JetStreamContext
from nats.js.api import RetentionPolicy, StorageType, StreamConfig
from nats.js.errors import BadRequestError

from nexus.bus.incident_event import IncidentEvent

logger = logging.getLogger(__name__)

# Constants

NEXUS_STREAM = "NEXUS_INCIDENTS"
NEXUS_SUBJECT = (
    "nexus.incidents"  # Subject prefix for incident events  (used by subscribe())
)
NEXUS_SUBJECTS = "nexus.>"  # Subject wildcard for ALL nexus.* events (stream bindings)
PPA_PREDICTIONS_SUBJECT = "ppa.predictions.>"  # PPA prediction events

# PPA owns its own stream; NEXUS subscribes to it via stream_name="PPA_PREDICTIONS".
# Mirror PPA's config so either side can create it without a config mismatch (NEXUS first is now safe).
PPA_STREAM = "PPA_PREDICTIONS"

# JetStream stream config: retain 24h of messages, max 50MB
_STREAM_CONFIG = StreamConfig(
    name=NEXUS_STREAM,
    subjects=[NEXUS_SUBJECTS],
    retention=RetentionPolicy.LIMITS,
    storage=StorageType.MEMORY,  # Use FILE in production
    max_age=86_400,  # 24 hours in seconds
    max_bytes=50 * 1024 * 1024,  # 50 MB
    duplicate_window=60,  # De-dupe window: 60s
)

# PPA prediction/outcome stream — mirror src/ppa/bus/nats_client.py payload layout.
_PPA_STREAM_CONFIG = StreamConfig(
    name=PPA_STREAM,
    subjects=["ppa.predictions.>", "ppa.outcomes.>"],
    retention=RetentionPolicy.LIMITS,
    storage=StorageType.MEMORY,
    max_age=3600,  # 1 hour
    max_bytes=10 * 1024 * 1024,  # 10 MB
)

HandlerType = Callable[[IncidentEvent], Awaitable[None]]

# Client
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
        self._url = nats_url
        self._connect_timeout = connect_timeout
        self._reconnect_attempts = reconnect_attempts
        self._nc: NATSConnection | None = None
        self._js: JetStreamContext | None = None
        self._subscribers: list[asyncio.Task] = []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

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
        await self._ensure_stream(_STREAM_CONFIG, NEXUS_STREAM)
        await self._ensure_stream(_PPA_STREAM_CONFIG, PPA_STREAM)
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

    # ── Stream management ─────────────────────────────────────────────────────

    async def _ensure_stream(self, config: StreamConfig, name: str) -> None:
        """
        Ensure a JetStream stream exists with the correct configuration.

        Strategy:
          1. Check if the stream already exists by name (js.stream_info).
             If it does, update it in-place to reconcile any subject drift.
          2. If the stream doesn't exist, create it (js.add_stream).
          3. If creation fails with err_code 10058 (name already in use with a
             different config), call update_stream() to reconcile.
          4. If creation fails with err_code 10065 (subjects overlap with another
             stream), call _resolve_subject_overlap() to evict the colliding
             subjects from the owning stream, then retry.  This is the permanent
             fix for the case where a stale NEXUS_INCIDENTS (created with
             subjects=[">"]) blocks creation of PPA_PREDICTIONS.
          5. Raise RuntimeError if the stream cannot be guaranteed to exist after
             all recovery attempts — never silently continue and crash later.

        nats-py 2.7 API:
          js.stream_info(name)                  — get stream info by name
                                                  (raises on NotFoundError / 10059)
          js.find_stream_name_by_subject(subj)  — find the stream owning a subject
          js.add_stream(config)                 — create stream
          js.update_stream(config)              — update existing stream config
        """
        # ─ Step 1: Check if stream exists; reconcile config if so. ────────────
        try:
            existing_info = await self._js.stream_info(name)
            existing_subjects = list(existing_info.config.subjects or [])
            desired_subjects = list(config.subjects or [])
            if existing_subjects != desired_subjects:
                logger.warning(
                    f"[NATS] Stream '{name}' has drifted subjects "
                    f"{existing_subjects} → {desired_subjects}. Updating."
                )
            try:
                await self._js.update_stream(config)
                logger.info(f"[NATS] Stream '{name}' config reconciled")
            except BadRequestError as upd_exc:
                # Some nats-py builds raise on a no-op update — safe to ignore.
                logger.debug(f"[NATS] Stream '{name}' update no-op: {upd_exc}")
            return
        except Exception as find_exc:
            exc_str = str(find_exc)
            if "10059" not in exc_str and "not found" not in exc_str.lower():
                # Unexpected error — log it but attempt creation below anyway.
                logger.warning(f"[NATS] stream_info('{name}') raised: {find_exc}")
            # NotFoundError (10059) means stream doesn't exist yet — fall through.

        # ─ Step 2: Create the stream. ─────────────────────────────────────────
        try:
            await self._js.add_stream(config)
            logger.info(f"[NATS] Created stream '{name}'")
            return
        except BadRequestError as exc:
            exc_str = str(exc)

            # err_code 10058 — name already in use with a different config.
            if "10058" in exc_str or "already in use" in exc_str:
                try:
                    await self._js.update_stream(config)
                    logger.info(f"[NATS] Updated stream '{name}' to current config")
                    return
                except Exception as upd_exc:
                    raise RuntimeError(
                        f"[NATS] Stream '{name}' exists with wrong config and "
                        f"update failed: {upd_exc}"
                    ) from upd_exc

            # err_code 10065 — subjects overlap with a pre-existing stream.
            if "10065" in exc_str or "subjects overlap" in exc_str:
                await self._resolve_subject_overlap(config, name)
                return

            raise RuntimeError(
                f"[NATS] Stream '{name}' creation failed: {exc}"
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                f"[NATS] Stream '{name}' creation failed unexpectedly: {exc}"
            ) from exc

    async def _resolve_subject_overlap(self, config: StreamConfig, name: str) -> None:
        """
        Evict subjects declared in *config* from whichever stream currently
        claims them, then retry creating the stream.

        This is the permanent fix for the scenario where NEXUS_INCIDENTS was
        previously created with subjects=[">"] (all subjects) instead of the
        correct ["nexus.>"].  That stale config causes every subsequent attempt
        to create PPA_PREDICTIONS to fail with err_code 10065.  We narrow the
        offending stream's subject list in-place — no messages are dropped —
        and then create the new stream cleanly.

        Uses js.find_stream_name_by_subject(subject) — the correct nats-py 2.7
        API that resolves a subject string to the name of the owning stream.
        """
        desired_subjects = list(config.subjects or [])
        assert (
            desired_subjects
        ), f"[NATS] Config for '{name}' has no subjects — cannot resolve overlap"

        for subject in desired_subjects:
            try:
                owner_name = await self._js.find_stream_name_by_subject(subject)
            except Exception:
                # Subject not claimed by any stream — nothing to resolve here.
                continue

            if owner_name == name:
                continue  # Already owned by the stream we're trying to create.

            logger.warning(
                f"[NATS] Subject '{subject}' is claimed by stream '{owner_name}' "
                f"instead of '{name}'. Evicting from '{owner_name}'."
            )
            try:
                owner_info = await self._js.stream_info(owner_name)
                current_subjects: list[str] = list(owner_info.config.subjects or [])
                corrected = [s for s in current_subjects if s not in desired_subjects]
                if not corrected:
                    raise RuntimeError(
                        f"[NATS] Resolving subject overlap would leave stream "
                        f"'{owner_name}' with no subjects. "
                        f"Current subjects: {current_subjects}. "
                        "Manual intervention required — delete or reconfigure the stream."
                    )
                owner_info.config.subjects = corrected
                await self._js.update_stream(owner_info.config)
                logger.info(
                    f"[NATS] Evicted overlapping subjects from '{owner_name}': "
                    f"{current_subjects} → {corrected}"
                )
            except RuntimeError:
                raise
            except Exception as fix_exc:
                raise RuntimeError(
                    f"[NATS] Could not evict overlapping subjects from "
                    f"'{owner_name}': {fix_exc}"
                ) from fix_exc

        # Retry creation after evicting all overlapping subjects.
        try:
            await self._js.add_stream(config)
            logger.info(
                f"[NATS] Created stream '{name}' after resolving subject overlap"
            )
        except Exception as retry_exc:
            raise RuntimeError(
                f"[NATS] Stream '{name}' still failed after subject overlap "
                f"resolution: {retry_exc}"
            ) from retry_exc

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
        logger.debug(
            f"[NATS] Published {subject} seq={ack.seq} event_id={event.event_id}"
        )

    # Alias for backwards compat with plan references
    async def publish_incident(self, event: IncidentEvent) -> None:
        await self.publish(event)

    async def publish_raw(self, subject: str, payload: dict) -> None:
        """
        Publish a raw dict payload to a non-IncidentEvent subject on the bus.

        Counterpart to ``subscribe_raw()``. Used by governance/notifications
        for events that are not IncidentEvents — e.g. ``nexus.approvals.required``
        to alert the Notifier of a pending L3 human-approval action.

        The subject must be covered by an existing JetStream stream binding
        (``nexus.>`` is bound to NEXUS_INCIDENTS by default).

        Args:
            subject: Full NATS subject, e.g. ``"nexus.approvals.required"``.
            payload: JSON-serializable dict.
        """
        if not self._js:
            raise RuntimeError("NATSClient not connected. Call connect() first.")
        import json

        data = json.dumps(payload).encode("utf-8")
        ack = await self._js.publish(subject, data, stream=NEXUS_STREAM)
        logger.debug(f"[NATS] Published raw {subject} seq={ack.seq}")

    # ── Subscribe ─────────────────────────────────────────────────────────────

    async def subscribe(
        self,
        handler: HandlerType,
        agent_filter: str = ">",
        signal_filter: str = ">",
        durable_name: str | None = None,
        queue_group: str | None = None,
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

        sub_kwargs = {"stream": NEXUS_STREAM}
        if durable_name:
            sub_kwargs["durable"] = durable_name
        if queue_group:
            sub_kwargs["queue"] = queue_group

        sub = await self._js.subscribe(subject, **sub_kwargs)

        async def _run_subscription():
            try:
                async for msg in sub.messages:
                    try:
                        event = IncidentEvent.from_nats_payload(msg.data)
                        await handler(event)
                        await msg.ack()
                    except Exception as exc:
                        logger.error(
                            f"[NATS] Handler error on subject '{msg.subject}': {exc}",
                            exc_info=True,
                        )
                        await msg.nak()
            except asyncio.CancelledError:
                await sub.unsubscribe()

        task = asyncio.create_task(_run_subscription())
        self._subscribers.append(task)

    RawHandlerType = Callable[[dict, str], Awaitable[None]]  # (payload_dict, subject)

    async def subscribe_raw(
        self,
        subject_pattern: str,
        handler: RawHandlerType,
        durable_name: str | None = None,
        stream_name: str | None = None,
    ) -> None:
        """
        Subscribe to raw-dict NATS subjects (non-IncidentEvent payloads).
        Used for PPA prediction events on ``ppa.predictions.*``.

        Args:
            subject_pattern: Full NATS subject with optional wildcard, e.g.
                             ``ppa.predictions.>``
            handler:         Async callback receiving ``(payload: dict, subject: str)``.
            durable_name:    JetStream durable consumer name.
            stream_name:     Explicit JetStream stream to bind to.
                             Defaults to NEXUS_INCIDENTS when not provided.
                             Pass ``'PPA_PREDICTIONS'`` for ppa.predictions.* subjects.
        """
        if not self._js:
            raise RuntimeError("NATSClient not connected.")

        resolved_stream = stream_name or NEXUS_STREAM
        logger.info(
            f"[NATS] Subscribing to raw pattern '{subject_pattern}' durable={durable_name} stream={resolved_stream}"
        )

        sub_kwargs: dict = {"stream": resolved_stream}
        if durable_name:
            sub_kwargs["durable"] = durable_name

        sub = await self._js.subscribe(subject_pattern, **sub_kwargs)

        async def _run_raw_subscription():
            try:
                async for msg in sub.messages:
                    try:
                        import json

                        data = json.loads(msg.data.decode("utf-8"))
                        await handler(data, msg.subject)
                        await msg.ack()
                    except Exception as exc:
                        logger.error(
                            f"[NATS] Raw handler error on {msg.subject}: {exc}",
                            exc_info=True,
                        )
                        await msg.nak()
            except asyncio.CancelledError:
                await sub.unsubscribe()

        task = asyncio.create_task(_run_raw_subscription())
        self._subscribers.append(task)

    # _keep_alive removed — subscriptions now use async-iterator tasks directly.
    # (nats-py >= 2.7 dropped the cb= keyword from js.subscribe)

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
