"""
Phase 7 — Tool Layer: DynamoDB Tools
=======================================
boto3 actions for DynamoDB. Pure functions — no AI, no decisions.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def get_table_info(dynamodb_client, table_name: str) -> dict[str, Any]:
    """Describe a DynamoDB table — billing mode, capacity, item count."""
    try:
        resp = dynamodb_client.describe_table(TableName=table_name)
        table = resp.get("Table", {})
        provisioned = table.get("ProvisionedThroughput", {})
        return {
            "table_name":    table.get("TableName"),
            "status":        table.get("TableStatus"),
            "billing_mode":  table.get("BillingModeSummary", {}).get("BillingMode", "PROVISIONED"),
            "item_count":    table.get("ItemCount", 0),
            "table_size_bytes": table.get("TableSizeBytes", 0),
            "read_capacity":  provisioned.get("ReadCapacityUnits", 0),
            "write_capacity": provisioned.get("WriteCapacityUnits", 0),
        }
    except Exception as exc:
        logger.warning(f"[DynamoDB] get_table_info({table_name}): {exc}")
        return {}


def update_capacity(
    dynamodb_client,
    table_name: str,
    read_capacity: int | None = None,
    write_capacity: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Update provisioned read/write capacity units.
    Only call this for PROVISIONED billing mode tables.
    """
    info = get_table_info(dynamodb_client, table_name)
    pre = {
        "read_capacity":  info.get("read_capacity"),
        "write_capacity": info.get("write_capacity"),
    }

    if info.get("billing_mode") == "PAY_PER_REQUEST":
        return {
            "success": False,
            "error": "Table is on-demand (PAY_PER_REQUEST) — capacity is managed automatically.",
            "pre": pre, "post": {},
        }

    new_read  = max(1, read_capacity or pre.get("read_capacity") or 1)
    new_write = max(1, write_capacity or pre.get("write_capacity") or 1)

    if dry_run:
        logger.info(f"[DRY-RUN] Would update {table_name} capacity: read={new_read}, write={new_write}")
        return {"success": True, "dry_run": True, "pre": pre, "post": {"read_capacity": new_read, "write_capacity": new_write}}

    try:
        dynamodb_client.update_table(
            TableName=table_name,
            ProvisionedThroughput={"ReadCapacityUnits": new_read, "WriteCapacityUnits": new_write},
        )
        logger.info(f"[DynamoDB] Updated {table_name}: read={new_read}, write={new_write}")
        return {"success": True, "error": None, "pre": pre, "post": {"read_capacity": new_read, "write_capacity": new_write}}
    except Exception as exc:
        logger.error(f"[DynamoDB] update_capacity({table_name}): {exc}")
        return {"success": False, "error": str(exc), "pre": pre, "post": {}}


def switch_to_on_demand(
    dynamodb_client,
    table_name: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Switch a PROVISIONED table to PAY_PER_REQUEST.
    Eliminates throttling immediately (costs more at high volume).
    """
    info = get_table_info(dynamodb_client, table_name)
    if info.get("billing_mode") == "PAY_PER_REQUEST":
        return {"success": True, "error": None, "note": "Already on-demand — no change needed."}

    if dry_run:
        logger.info(f"[DRY-RUN] Would switch {table_name} to PAY_PER_REQUEST")
        return {"success": True, "dry_run": True}

    try:
        dynamodb_client.update_table(TableName=table_name, BillingMode="PAY_PER_REQUEST")
        logger.info(f"[DynamoDB] Switched {table_name} to on-demand billing")
        return {"success": True, "error": None, "pre": {"billing_mode": "PROVISIONED"}, "post": {"billing_mode": "PAY_PER_REQUEST"}}
    except Exception as exc:
        logger.error(f"[DynamoDB] switch_to_on_demand({table_name}): {exc}")
        return {"success": False, "error": str(exc)}
