"""Subscription loops and message deserialization for NATS JetStream."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from nats.js import JetStreamContext
from nats.js.api import DeliverPolicy

from nexus.bus.events.event import IncidentEvent
from nexus.bus.nats.constants import NEXUS_STREAM, NEXUS_SUBJECT

logger = logging.getLogger(__name__)

HandlerType = Callable[[IncidentEvent], Awaitable[None]]
RawHandlerType = Callable[[dict, str], Awaitable[None]]


async def create_incident_subscription(
    js: JetStreamContext,
    handler: HandlerType,
    agent_filter: str = ">",
    signal_filter: str = ">",
    durable_name: str | None = None,
    queue_group: str | None = None,
    deliver_policy: DeliverPolicy = DeliverPolicy.NEW,
) -> asyncio.Task:
    """Create background task subscribing to typed IncidentEvent subjects."""
    subject = f"{NEXUS_SUBJECT}.{agent_filter}"
    if signal_filter != ">":
        subject = f"{NEXUS_SUBJECT}.{agent_filter}.{signal_filter}"

    logger.info(f"[NATS] Subscribing to '{subject}' durable={durable_name}")
    sub_kwargs: dict = {"stream": NEXUS_STREAM, "deliver_policy": deliver_policy}
    if durable_name:
        sub_kwargs["durable"] = durable_name
    if queue_group:
        sub_kwargs["queue"] = queue_group

    sub = await js.subscribe(subject, **sub_kwargs)

    async def _run():
        try:
            async for msg in sub.messages:
                try:
                    event = IncidentEvent.from_nats_payload(msg.data)
                except Exception as parse_exc:
                    logger.warning(
                        f"[NATS] Dropping unparseable message on '{msg.subject}': {parse_exc}"
                    )
                    try:
                        if hasattr(msg, "term"):
                            await msg.term()
                        else:
                            await msg.ack()
                    except Exception:
                        await msg.ack()
                    continue

                try:
                    await handler(event)
                    await msg.ack()
                except Exception as exc:
                    logger.error(
                        f"[NATS] Handler error on '{msg.subject}': {exc}", exc_info=True
                    )
                    await msg.nak()
        except asyncio.CancelledError:
            await sub.unsubscribe()

    return asyncio.create_task(_run())


async def create_raw_subscription(
    js: JetStreamContext,
    subject_pattern: str,
    handler: RawHandlerType,
    durable_name: str | None = None,
    stream_name: str | None = None,
    deliver_policy: DeliverPolicy = DeliverPolicy.NEW,
) -> asyncio.Task:
    """Create background task subscribing to raw JSON payloads."""
    resolved_stream = stream_name or NEXUS_STREAM
    logger.info(
        f"[NATS] Subscribing to raw pattern '{subject_pattern}' durable={durable_name} stream={resolved_stream}"
    )

    sub_kwargs: dict = {"stream": resolved_stream, "deliver_policy": deliver_policy}
    if durable_name:
        sub_kwargs["durable"] = durable_name

    sub = await js.subscribe(subject_pattern, **sub_kwargs)

    async def _run():
        try:
            async for msg in sub.messages:
                try:
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

    return asyncio.create_task(_run())
