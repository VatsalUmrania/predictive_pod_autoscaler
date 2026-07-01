"""
Phase 7 — Tool Layer: API Gateway Tools
=========================================
boto3 queries for API Gateway REST and HTTP APIs.
Covers stage throttling, caching, and CloudWatch integration checks.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def get_stage_info(apigw_client, rest_api_id: str, stage_name: str = "prod") -> dict[str, Any]:
    """Fetch API Gateway stage configuration — throttling limits, logging, caching."""
    try:
        resp = apigw_client.get_stage(restApiId=rest_api_id, stageName=stage_name)
        throttle = resp.get("defaultRouteSettings", resp.get("methodSettings", {}).get("*/*", {}))
        return {
            "stage_name":          resp.get("stageName"),
            "description":         resp.get("description", ""),
            "burst_limit":         resp.get("defaultRouteSettings", {}).get("throttlingBurstLimit", 0),
            "rate_limit":          resp.get("defaultRouteSettings", {}).get("throttlingRateLimit", 0),
            "caching_enabled":     resp.get("cacheClusterEnabled", False),
            "logging_level":       resp.get("accessLogSettings", {}).get("destinationArn", ""),
            "last_updated_date":   str(resp.get("lastUpdatedDate", "")),
        }
    except Exception as exc:
        logger.debug(f"[APIGateway] get_stage_info({rest_api_id}/{stage_name}): {exc}")
        return {}


def get_api_keys(apigw_client) -> list[dict]:
    """List active API keys (to check if a key was recently rotated)."""
    keys = []
    try:
        resp = apigw_client.get_api_keys(includeValues=False)
        for key in resp.get("items", []):
            keys.append({
                "id":          key.get("id"),
                "name":        key.get("name"),
                "enabled":     key.get("enabled"),
                "created_date": str(key.get("createdDate", "")),
            })
    except Exception as exc:
        logger.debug(f"[APIGateway] get_api_keys: {exc}")
    return keys


def update_stage_throttle(
    apigw_client,
    rest_api_id: str,
    stage_name: str,
    burst_limit: int,
    rate_limit: float,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Update API Gateway stage throttle limits.
    Useful when a burst of traffic is causing 429 errors.
    """
    if dry_run:
        logger.info(f"[DRY-RUN] Would update throttle: {rest_api_id}/{stage_name} burst={burst_limit} rate={rate_limit}")
        return {"success": True, "dry_run": True}

    try:
        apigw_client.update_stage(
            restApiId=rest_api_id,
            stageName=stage_name,
            patchOperations=[
                {"op": "replace", "path": "/defaultRouteSettings/throttlingBurstLimit", "value": str(burst_limit)},
                {"op": "replace", "path": "/defaultRouteSettings/throttlingRateLimit", "value": str(rate_limit)},
            ],
        )
        return {"success": True, "error": None, "burst_limit": burst_limit, "rate_limit": rate_limit}
    except Exception as exc:
        logger.error(f"[APIGateway] update_stage_throttle: {exc}")
        return {"success": False, "error": str(exc)}
