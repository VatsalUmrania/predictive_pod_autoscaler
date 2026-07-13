"""
Approval Store
===============
DynamoDB-backed store for pending command approvals.

When the LLM detects an error and suggests boto3 commands to fix it,
those commands are stored here as a "pending" approval.
The user is notified on Slack. When they approve via the API endpoint,
the commands are fetched from here, validated, and executed.

DynamoDB schema:
  PK:           approval_id  (UUID string)
  status:       pending | approved | rejected | executed | failed
  commands:     JSON-encoded list of boto3 call specs
  function_name: the Lambda that had the error
  root_cause:   brief description of the error
  analysis:     full LLM analysis JSON
  created_at:   ISO timestamp
  executed_at:  ISO timestamp (set when status changes from pending)
  results:      JSON-encoded list of execution results
  ttl:          Unix timestamp — DynamoDB auto-deletes after 24h
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

TABLE_NAME = os.environ.get("APPROVALS_TABLE_NAME", "nexus-ai-agent-approvals")


def create_approval(
    commands: list[dict[str, Any]],
    function_name: str,
    root_cause: str,
    analysis: dict[str, Any] | None = None,
    region: str = "us-east-1",
) -> str:
    """
    Store a pending approval in DynamoDB.
    Returns the approval_id (UUID string).
    """
    approval_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    ttl = int((now + timedelta(hours=24)).timestamp())

    item: dict[str, Any] = {
        "approval_id": approval_id,
        "status": "pending",
        "commands": json.dumps(commands),
        "function_name": function_name,
        "root_cause": root_cause,
        "created_at": now.isoformat(),
        "ttl": ttl,
    }
    if analysis:
        # Store minimal analysis to avoid exceeding DynamoDB item size
        item["analysis"] = json.dumps(
            {
                "severity": analysis.get("severity"),
                "action": analysis.get("action"),
                "confidence": analysis.get("confidence"),
                "error_types": analysis.get("error_types", []),
            }
        )

    try:
        dynamodb = boto3.resource("dynamodb", region_name=region)
        table = dynamodb.Table(TABLE_NAME)
        table.put_item(Item=item)
        logger.info(f"[Approvals] Created approval {approval_id} for {function_name}")
    except ClientError as exc:
        # Log but don't raise — the approval_id is still returned so Slack
        # can show it even if DynamoDB isn't available yet
        logger.error(f"[Approvals] DynamoDB write failed: {exc}")

    return approval_id


def get_approval(approval_id: str, region: str = "us-east-1") -> dict[str, Any] | None:
    """
    Fetch an approval record by ID.
    Returns None if not found or on error.
    Deserializes the 'commands' and 'analysis' JSON fields.
    """
    try:
        dynamodb = boto3.resource("dynamodb", region_name=region)
        table = dynamodb.Table(TABLE_NAME)
        resp = table.get_item(Key={"approval_id": approval_id})
        item = resp.get("Item")
        if not item:
            return None
        # Deserialize JSON fields
        item["commands"] = json.loads(item.get("commands", "[]"))
        if "analysis" in item:
            item["analysis"] = json.loads(item["analysis"])
        return item
    except ClientError as exc:
        logger.error(f"[Approvals] DynamoDB get failed for {approval_id}: {exc}")
        return None


def update_status(
    approval_id: str,
    status: str,
    results: list[dict] | None = None,
    region: str = "us-east-1",
) -> bool:
    """
    Transition an approval to a new status.
    Valid transitions: pending → approved → executed | failed | rejected
    """
    try:
        dynamodb = boto3.resource("dynamodb", region_name=region)
        table = dynamodb.Table(TABLE_NAME)

        update_expr = "SET #s = :s, executed_at = :t"
        attr_names = {"#s": "status"}
        attr_values: dict[str, Any] = {
            ":s": status,
            ":t": datetime.now(timezone.utc).isoformat(),
        }

        if results is not None:
            update_expr += ", results = :r"
            attr_values[":r"] = json.dumps(results)

        table.update_item(
            Key={"approval_id": approval_id},
            UpdateExpression=update_expr,
            ExpressionAttributeNames=attr_names,
            ExpressionAttributeValues=attr_values,
        )
        logger.info(f"[Approvals] {approval_id} → {status}")
        return True

    except ClientError as exc:
        logger.error(f"[Approvals] Failed to update {approval_id}: {exc}")
        return False
