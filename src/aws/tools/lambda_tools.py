"""
Lambda Remediation Tools
=========================
boto3 actions for remediating Lambda incidents.
Pure functions — take a boto3 client and params, return a result dict.

Result dict always has:
    success: bool
    error:   str | None
    pre:     dict   (state before action)
    post:    dict   (state after action)
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def get_function_config(lambda_client, function_name: str) -> dict[str, Any]:
    """Fetch current Lambda configuration. Returns {} on error."""
    try:
        return lambda_client.get_function_configuration(FunctionName=function_name)
    except Exception as exc:
        logger.warning(f"[LambdaTools] get_function_config({function_name}): {exc}")
        return {}


def get_published_versions(lambda_client, function_name: str) -> list[dict]:
    """
    Return published versions sorted oldest → newest (excludes $LATEST).
    Returns [] on error.
    """
    try:
        resp = lambda_client.list_versions_by_function(
            FunctionName=function_name, MaxItems=20
        )
        versions = [
            v for v in resp.get("Versions", [])
            if v.get("Version") != "$LATEST"
        ]
        return sorted(versions, key=lambda v: v.get("LastModified", ""))
    except Exception as exc:
        logger.warning(f"[LambdaTools] get_published_versions({function_name}): {exc}")
        return []


def increase_memory(
    lambda_client,
    function_name: str,
    memory_mb: int,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Update Lambda memory size.
    memory_mb is clamped to [128, 3008] (AWS limits).
    """
    memory_mb = max(128, min(3008, memory_mb))
    config = get_function_config(lambda_client, function_name)
    pre_memory = config.get("MemorySize", 128)

    if dry_run:
        logger.info(f"[DRY-RUN] Would set {function_name} memory: {pre_memory}MB → {memory_mb}MB")
        return {"success": True, "dry_run": True, "pre": {"memory_mb": pre_memory}, "post": {"memory_mb": memory_mb}}

    try:
        lambda_client.update_function_configuration(
            FunctionName=function_name,
            MemorySize=memory_mb,
        )
        logger.info(f"[LambdaTools] {function_name}: memory {pre_memory}MB → {memory_mb}MB")
        return {
            "success": True, "error": None,
            "pre": {"memory_mb": pre_memory},
            "post": {"memory_mb": memory_mb},
        }
    except Exception as exc:
        logger.error(f"[LambdaTools] increase_memory({function_name}): {exc}")
        return {"success": False, "error": str(exc), "pre": {"memory_mb": pre_memory}, "post": {}}


def increase_timeout(
    lambda_client,
    function_name: str,
    timeout_seconds: int,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Update Lambda timeout.
    timeout_seconds is clamped to [1, 900] (AWS limits).
    """
    timeout_seconds = max(1, min(900, timeout_seconds))
    config = get_function_config(lambda_client, function_name)
    pre_timeout = config.get("Timeout", 3)

    if dry_run:
        logger.info(f"[DRY-RUN] Would set {function_name} timeout: {pre_timeout}s → {timeout_seconds}s")
        return {"success": True, "dry_run": True, "pre": {"timeout_seconds": pre_timeout}, "post": {"timeout_seconds": timeout_seconds}}

    try:
        lambda_client.update_function_configuration(
            FunctionName=function_name,
            Timeout=timeout_seconds,
        )
        logger.info(f"[LambdaTools] {function_name}: timeout {pre_timeout}s → {timeout_seconds}s")
        return {
            "success": True, "error": None,
            "pre": {"timeout_seconds": pre_timeout},
            "post": {"timeout_seconds": timeout_seconds},
        }
    except Exception as exc:
        logger.error(f"[LambdaTools] increase_timeout({function_name}): {exc}")
        return {"success": False, "error": str(exc), "pre": {"timeout_seconds": pre_timeout}, "post": {}}


def rollback_alias(
    lambda_client,
    function_name: str,
    alias_name: str = "live",
    target_version: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Roll back a Lambda alias to the previous published version.

    If target_version is not given, auto-detects by finding the version
    before the one the alias currently points to.
    """
    try:
        # Get current alias target
        alias_resp = lambda_client.get_alias(FunctionName=function_name, Name=alias_name)
        current_version = alias_resp.get("FunctionVersion", "$LATEST")
    except Exception as exc:
        # Alias doesn't exist — nothing to roll back
        logger.warning(f"[LambdaTools] rollback_alias: alias '{alias_name}' not found on {function_name}: {exc}")
        return {"success": False, "error": f"Alias '{alias_name}' not found: {exc}", "pre": {}, "post": {}}

    # Auto-detect previous version
    if not target_version:
        versions = get_published_versions(lambda_client, function_name)
        try:
            current_idx = next(
                i for i, v in enumerate(versions)
                if v.get("Version") == current_version
            )
        except StopIteration:
            current_idx = len(versions)

        if current_idx <= 0:
            return {
                "success": False,
                "error": "No previous published version to roll back to. "
                         "Publish at least 2 versions before rollback is possible.",
                "pre": {"alias": alias_name, "version": current_version},
                "post": {},
            }
        target_version = versions[current_idx - 1]["Version"]

    if dry_run:
        logger.info(f"[DRY-RUN] Would roll back {function_name} alias '{alias_name}': {current_version} → {target_version}")
        return {
            "success": True, "dry_run": True,
            "pre": {"alias": alias_name, "version": current_version},
            "post": {"alias": alias_name, "version": target_version},
        }

    try:
        lambda_client.update_alias(
            FunctionName=function_name,
            Name=alias_name,
            FunctionVersion=target_version,
            Description=f"NEXUS AI rollback from v{current_version} to v{target_version}",
        )
        logger.info(f"[LambdaTools] {function_name}: alias '{alias_name}' {current_version} → {target_version}")
        return {
            "success": True, "error": None,
            "pre": {"alias": alias_name, "version": current_version},
            "post": {"alias": alias_name, "version": target_version},
        }
    except Exception as exc:
        logger.error(f"[LambdaTools] rollback_alias({function_name}): {exc}")
        return {
            "success": False, "error": str(exc),
            "pre": {"alias": alias_name, "version": current_version},
            "post": {},
        }


def set_reserved_concurrency(
    lambda_client,
    function_name: str,
    reserved_concurrent_executions: int,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Set reserved concurrency for a Lambda function (clamped to [0, 1000])."""
    reserved = max(0, min(1000, reserved_concurrent_executions))

    if dry_run:
        logger.info(f"[DRY-RUN] Would set {function_name} reserved concurrency to {reserved}")
        return {"success": True, "dry_run": True, "pre": {}, "post": {"reserved_concurrent_executions": reserved}}

    try:
        lambda_client.put_function_concurrency(
            FunctionName=function_name,
            ReservedConcurrentExecutions=reserved,
        )
        logger.info(f"[LambdaTools] {function_name}: reserved concurrency = {reserved}")
        return {
            "success": True, "error": None,
            "pre": {}, "post": {"reserved_concurrent_executions": reserved},
        }
    except Exception as exc:
        logger.error(f"[LambdaTools] set_reserved_concurrency({function_name}): {exc}")
        return {"success": False, "error": str(exc), "pre": {}, "post": {}}


def list_all_functions(lambda_client, prefix: str = "") -> list[dict[str, Any]]:
    """
    Return all Lambda functions, optionally filtered by prefix.
    Returns list of function config dicts from AWS.
    """
    functions = []
    try:
        paginator = lambda_client.get_paginator("list_functions")
        for page in paginator.paginate():
            for fn in page.get("Functions", []):
                name = fn.get("FunctionName", "")
                if not prefix or name.startswith(prefix):
                    functions.append(fn)
    except Exception as exc:
        logger.warning(f"[LambdaTools] list_all_functions: {exc}")
    return functions
