"""
Phase 9 — Memory: Incident Store (DynamoDB)
=============================================
Stores every incident and its outcome in DynamoDB.
The AI searches previous incidents before reasoning to avoid repeating mistakes.

DynamoDB table schema:
  pk (S): "incident#{resource}#{timestamp}"
  resource_name (S)
  signal_type (S)
  root_cause (S)
  action_taken (S)
  resolved (BOOL)
  confidence (N)
  duration_seconds (N)
  timestamp (S) ISO-8601
  region (S)
  rca_json (S)  ← full RCA dict stored as JSON string

For vector search (Phase 9 advanced), see vector_store.py.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import boto3

logger = logging.getLogger(__name__)

_TABLE_NAME = os.getenv("INCIDENT_TABLE_NAME", "nexus-ai-agent-incidents")


def save_incident(
    resource_name: str,
    signal_type: str,
    rca: dict[str, Any],
    action_result: dict[str, Any],
    resolved: bool,
    duration_seconds: float,
    region: str = "us-east-1",
    table_name: str = _TABLE_NAME,
) -> bool:
    """
    Save a completed incident to DynamoDB.

    Args:
        resource_name:    Lambda/DynamoDB/SQS resource
        signal_type:      e.g. "lambda_error_rate_high"
        rca:              Diagnosis output dict
        action_result:    Execution result dict
        resolved:         True if incident was resolved
        duration_seconds: How long the full pipeline took
        region:           AWS region
        table_name:       DynamoDB table name

    Returns True on success, False on error.
    """
    now = datetime.now(timezone.utc)
    pk = f"incident#{resource_name}#{now.isoformat()}"

    item = {
        "pk":               {"S": pk},
        "resource_name":    {"S": resource_name},
        "signal_type":      {"S": signal_type},
        "root_cause":       {"S": rca.get("root_cause", "unknown")[:500]},
        "failure_class":    {"S": rca.get("failure_class", "unknown")},
        "action_taken":     {"S": rca.get("action", "alert_only")},
        "resolved":         {"BOOL": resolved},
        "confidence":       {"N": str(round(float(rca.get("confidence", 0.0)), 4))},
        "severity":         {"S": rca.get("severity", "Medium")},
        "duration_seconds": {"N": str(round(duration_seconds, 1))},
        "timestamp":        {"S": now.isoformat()},
        "region":           {"S": region},
        "rca_json":         {"S": json.dumps(rca)[:4000]},
        "action_result_json": {"S": json.dumps(action_result)[:2000]},
    }

    try:
        ddb = boto3.client("dynamodb", region_name=region)
        ddb.put_item(TableName=table_name, Item=item)
        logger.info(f"[Memory] Saved incident: {pk} (resolved={resolved})")
        return True
    except Exception as exc:
        logger.warning(f"[Memory] Failed to save incident: {exc}")
        return False


def get_recent_incidents(
    resource_name: str,
    limit: int = 5,
    region: str = "us-east-1",
    table_name: str = _TABLE_NAME,
) -> list[dict[str, Any]]:
    """
    Retrieve recent incidents for a resource.
    Used to give Claude context about past failures and what worked.
    """
    try:
        ddb = boto3.client("dynamodb", region_name=region)
        resp = ddb.query(
            TableName=table_name,
            IndexName="resource-timestamp-index",   # GSI required
            KeyConditionExpression="resource_name = :r",
            ExpressionAttributeValues={":r": {"S": resource_name}},
            ScanIndexForward=False,   # newest first
            Limit=limit,
        )
        incidents = []
        for item in resp.get("Items", []):
            incidents.append({
                "timestamp":    item.get("timestamp", {}).get("S", ""),
                "root_cause":   item.get("root_cause", {}).get("S", ""),
                "action_taken": item.get("action_taken", {}).get("S", ""),
                "resolved":     item.get("resolved", {}).get("BOOL", False),
                "confidence":   float(item.get("confidence", {}).get("N", "0")),
            })
        return incidents
    except Exception as exc:
        logger.debug(f"[Memory] get_recent_incidents({resource_name}): {exc}")
        return []


def format_memory_for_prompt(incidents: list[dict]) -> str:
    """Format past incidents into a string for injection into the Claude prompt."""
    if not incidents:
        return "No previous incidents found for this resource."
    lines = ["## Previous Incidents (most recent first)"]
    for inc in incidents:
        status = "✅ RESOLVED" if inc.get("resolved") else "❌ UNRESOLVED"
        lines.append(
            f"- [{inc.get('timestamp', '')[:19]}] {status} | "
            f"Root cause: {inc.get('root_cause', 'unknown')} | "
            f"Action: {inc.get('action_taken', 'unknown')} | "
            f"Confidence: {float(inc.get('confidence', 0)):.0%}"
        )
    return "\n".join(lines)
