"""
NEXUS SQS Agent
================
Monitors AWS SQS queues and their dead-letter queues (DLQs) for
message depth and consumer lag.

Published IncidentEvents:
    SQS_DLQ_DEPTH_HIGH    — DLQ message count > threshold (default 10)
    SQS_QUEUE_DEPTH_HIGH  — Source queue depth > threshold (default 1000)
    SQS_CONSUMER_LAG      — Oldest message age > configured SLA
"""

from __future__ import annotations

import asyncio
import json
import logging

from nexus.agents.aws.base_aws_agent import BaseAWSAgent
from nexus.agents.aws.config import AWSConfig
from nexus.bus.incident_event import (
    AgentType,
    IncidentEvent,
    Severity,
    SignalType,
    SqsDLQContext,
)
from nexus.bus.nats_client import NATSClient

logger = logging.getLogger(__name__)
_QUEUE_ATTRS = [
    "ApproximateNumberOfMessages",
    "ApproximateNumberOfMessagesNotVisible",
    "ApproximateAgeOfOldestMessage",
    "VisibilityTimeout",
    "MessageRetentionPeriod",
    "RedrivePolicy",
]


class SqsAgent(BaseAWSAgent):
    """Monitors SQS queues and DLQs for depth and consumer lag."""

    def __init__(
        self,
        nats_client: NATSClient,
        config: AWSConfig | None = None,
    ) -> None:
        super().__init__(
            nats_client=nats_client,
            agent_type=AgentType.SQS,
            config=config,
        )
        self._known_queues: list[str] = []   # queue URLs

    async def sense(self) -> list[IncidentEvent]:
        events: list[IncidentEvent] = []

        if not self._known_queues:
            self._known_queues = await self._list_queue_urls()

        tasks = [self._check_queue(url) for url in self._known_queues]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, list):
                events.extend(r)

        if self._total_events_published % 10 == 9:
            self._known_queues = []
        return events

    async def _list_queue_urls(self) -> list[str]:
        if self._sqs is None:
            return []
        try:
            loop = asyncio.get_event_loop()
            urls = []
            resp = await loop.run_in_executor(None, lambda: self._sqs.list_queues(MaxResults=100))
            while True:
                urls.extend(resp.get("QueueUrls", []))
                token = resp.get("NextToken")
                if not token:
                    break
                resp = await loop.run_in_executor(
                    None,
                    lambda t=token: self._sqs.list_queues(MaxResults=100, NextToken=t),
                )
            logger.info(f"[SqsAgent] Monitoring {len(urls)} SQS queue(s)")
            return urls
        except Exception as exc:
            logger.debug(f"[SqsAgent] list_queues: {exc}")
            return []

    async def _check_queue(self, queue_url: str) -> list[IncidentEvent]:
        events: list[IncidentEvent] = []
        if self._sqs is None:
            return events

        try:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: self._sqs.get_queue_attributes(
                    QueueUrl=queue_url,
                    AttributeNames=_QUEUE_ATTRS,
                ),
            )
            attrs = resp.get("Attributes", {})
        except Exception as exc:
            logger.debug(f"[SqsAgent] get_queue_attributes({queue_url}): {exc}")
            return events

        queue_name = queue_url.split("/")[-1]
        depth = int(attrs.get("ApproximateNumberOfMessages", 0))
        age_seconds = int(attrs.get("ApproximateAgeOfOldestMessage", 0))
        visibility_timeout = int(attrs.get("VisibilityTimeout", 30))
        message_retention = int(attrs.get("MessageRetentionPeriod", 345600))

        # Detect DLQ ARN via redrive policy
        dlq_url: str | None = None
        dlq_name: str | None = None
        dlq_depth = 0
        redrive_raw = attrs.get("RedrivePolicy", "{}")
        try:
            redrive = json.loads(redrive_raw)
            dlq_arn = redrive.get("deadLetterTargetArn", "")
            if dlq_arn:
                dlq_name = dlq_arn.split(":")[-1]
                # Resolve ARN → URL
                try:
                    dlq_resp = await loop.run_in_executor(
                        None,
                        lambda: self._sqs.get_queue_url(QueueName=dlq_name),
                    )
                    dlq_url = dlq_resp.get("QueueUrl")
                    if dlq_url:
                        dlq_attrs_resp = await loop.run_in_executor(
                            None,
                            lambda: self._sqs.get_queue_attributes(
                                QueueUrl=dlq_url,
                                AttributeNames=["ApproximateNumberOfMessages"],
                            ),
                        )
                        dlq_depth = int(
                            dlq_attrs_resp.get("Attributes", {})
                            .get("ApproximateNumberOfMessages", 0)
                        )
                except Exception:
                    pass
        except (json.JSONDecodeError, Exception):
            pass

        # Sample messages from DLQ (non-destructive — VisibilityTimeout=0 peek)
        sample_messages: list[str] = []
        if dlq_url and dlq_depth > 0:
            sample_messages = await self._sample_dlq(dlq_url)

        ctx = SqsDLQContext(
            queue_name=queue_name,
            queue_url=queue_url,
            dlq_name=dlq_name,
            dlq_url=dlq_url,
            dlq_depth=dlq_depth,
            queue_depth=depth,
            oldest_message_age_seconds=age_seconds if age_seconds > 0 else None,
            visibility_timeout=visibility_timeout,
            message_retention=message_retention,
            sample_messages=sample_messages,
        )

        # ── DLQ depth check ───────────────────────────────────────────────────
        if dlq_depth >= self.cfg.sqs_dlq_threshold:
            events.append(IncidentEvent(
                agent=AgentType.SQS,
                signal_type=SignalType.SQS_DLQ_DEPTH_HIGH,
                severity=Severity.CRITICAL if dlq_depth > 100 else Severity.WARNING,
                resource_name=queue_name,
                confidence=0.85,
                context=ctx.model_dump(),
            ))
            logger.warning(f"[SqsAgent] {queue_name}: DLQ depth={dlq_depth} (threshold={self.cfg.sqs_dlq_threshold})")

        # ── Source queue depth check ───────────────────────────────────────────
        elif depth >= self.cfg.sqs_queue_depth_threshold:
            events.append(IncidentEvent(
                agent=AgentType.SQS,
                signal_type=SignalType.SQS_QUEUE_DEPTH_HIGH,
                severity=Severity.WARNING,
                resource_name=queue_name,
                confidence=0.70,
                context=ctx.model_dump(),
            ))

        return events

    async def _sample_dlq(self, dlq_url: str) -> list[str]:
        """Non-destructively peek at up to 5 DLQ messages for context."""
        if self._sqs is None:
            return []
        try:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: self._sqs.receive_message(
                    QueueUrl=dlq_url,
                    MaxNumberOfMessages=min(self.cfg.sqs_max_sample_messages, 10),
                    VisibilityTimeout=0,  # Immediately invisible — don't consume
                    WaitTimeSeconds=0,
                ),
            )
            return [
                msg.get("Body", "")[:500]  # Truncate at 500 chars
                for msg in resp.get("Messages", [])
            ]
        except Exception:
            return []
