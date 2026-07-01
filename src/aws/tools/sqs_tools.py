"""
SQS Remediation Tools
======================
Pure boto3 functions for SQS queue and DLQ operations.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def get_queue_depth(sqs_client, queue_url: str) -> int:
    """Return number of visible messages in a queue. Returns 0 on error."""
    try:
        resp = sqs_client.get_queue_attributes(
            QueueUrl=queue_url,
            AttributeNames=["ApproximateNumberOfMessages"],
        )
        return int(resp.get("Attributes", {}).get("ApproximateNumberOfMessages", 0))
    except Exception as exc:
        logger.debug(f"[SQSTools] get_queue_depth({queue_url}): {exc}")
        return 0


def sample_dlq_messages(
    sqs_client,
    dlq_url: str,
    max_messages: int = 5,
) -> list[dict[str, Any]]:
    """
    Peek at DLQ messages without deleting them (using long visibility timeout
    to prevent other readers from also receiving them, then immediately resets).

    Returns list of {body, attributes, message_id} dicts.
    """
    samples = []
    try:
        resp = sqs_client.receive_message(
            QueueUrl=dlq_url,
            MaxNumberOfMessages=min(max_messages, 10),
            VisibilityTimeout=0,    # Don't hide from other consumers
            WaitTimeSeconds=0,
            MessageAttributeNames=["All"],
        )
        for msg in resp.get("Messages", []):
            samples.append({
                "message_id": msg.get("MessageId", ""),
                "body": msg.get("Body", "")[:500],    # truncate long bodies
                "attributes": msg.get("MessageAttributes", {}),
            })
    except Exception as exc:
        logger.debug(f"[SQSTools] sample_dlq_messages({dlq_url}): {exc}")
    return samples


def replay_dlq(
    sqs_client,
    dlq_url: str,
    source_queue_url: str,
    max_messages: int = 100,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Move messages from DLQ back to the source queue for reprocessing.

    Process:
      1. Receive up to 10 messages from DLQ
      2. Send each to source_queue_url
      3. Delete from DLQ on successful send
      4. Repeat until max_messages reached or DLQ empty

    Returns:
        success:        bool
        messages_moved: int
        error:          str | None
    """
    if dry_run:
        depth = get_queue_depth(sqs_client, dlq_url)
        logger.info(f"[DRY-RUN] Would replay up to {max_messages} messages from DLQ (current depth: {depth})")
        return {"success": True, "dry_run": True, "messages_moved": 0, "error": None}

    moved = 0
    try:
        while moved < max_messages:
            resp = sqs_client.receive_message(
                QueueUrl=dlq_url,
                MaxNumberOfMessages=min(10, max_messages - moved),
                VisibilityTimeout=30,
                WaitTimeSeconds=0,
            )
            messages = resp.get("Messages", [])
            if not messages:
                break   # DLQ empty

            for msg in messages:
                try:
                    # Re-send to source queue
                    sqs_client.send_message(
                        QueueUrl=source_queue_url,
                        MessageBody=msg["Body"],
                    )
                    # Delete from DLQ only after successful send
                    sqs_client.delete_message(
                        QueueUrl=dlq_url,
                        ReceiptHandle=msg["ReceiptHandle"],
                    )
                    moved += 1
                except Exception as exc:
                    logger.warning(f"[SQSTools] Failed to replay message {msg.get('MessageId')}: {exc}")

        logger.info(f"[SQSTools] Replayed {moved} messages: {dlq_url} → {source_queue_url}")
        return {"success": True, "messages_moved": moved, "error": None}

    except Exception as exc:
        logger.error(f"[SQSTools] replay_dlq failed: {exc}")
        return {"success": False, "messages_moved": moved, "error": str(exc)}
