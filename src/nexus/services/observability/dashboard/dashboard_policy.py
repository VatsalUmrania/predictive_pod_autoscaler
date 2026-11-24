"""
Policy cache and overrides for the developer dashboard.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

_policy_overrides: dict[str, dict[str, Any]] = {}
_policy_cache: dict[str, dict[str, Any]] = {}


def cache_policy(app_name: str, policy_dict: dict[str, Any]) -> None:
    """Called by GitAgent when a new selfheal.yaml is processed."""
    _policy_cache[app_name] = policy_dict
    logger.info(f"[Dashboard] Policy cached for app='{app_name}'")


def refresh_policy_cache() -> int:
    """Eagerly scan filesystem for selfheal.yaml files and populate _policy_cache."""
    import yaml as _yaml

    candidates = [
        os.environ.get("NEXUS_SELFHEAL_YAML_PATH", ""),
        "/app/selfheal.yaml",
        "/data/selfheal.yaml",
    ]

    loaded = 0
    for path_str in candidates:
        if not path_str:
            continue
        p = Path(path_str)
        if not p.exists():
            continue
        try:
            data = _yaml.safe_load(p.read_text())
        except Exception as exc:
            logger.debug(f"[Dashboard] policy refresh read error at {p}: {exc}")
            continue
        if not isinstance(data, dict):
            continue
        app_name = data.get("app") or p.stem
        _policy_cache[app_name] = data
        loaded += 1
        logger.info(f"[Dashboard] Pre-cached policy for app='{app_name}' from {p}")

    if loaded == 0:
        logger.debug("[Dashboard] refresh_policy_cache found no selfheal.yaml files")
    return loaded


class PolicyOverride(BaseModel):
    field: str
    value: Any


def get_resolved_policy(app: str) -> dict[str, Any]:
    """Return the resolved policy for an app, checking cache and filesystem."""
    base = _policy_cache.get(app)
    override = _policy_overrides.get(app, {})

    if base is None:
        candidates = [
            os.environ.get("NEXUS_SELFHEAL_YAML_PATH", ""),
            "/app/selfheal.yaml",
            "/data/selfheal.yaml",
        ]
        for path_str in candidates:
            if not path_str:
                continue
            p = Path(path_str)
            if p.exists():
                try:
                    import yaml as _yaml

                    data = _yaml.safe_load(p.read_text())
                    if isinstance(data, dict):
                        file_app = data.get("app", app)
                        if file_app == app or app not in _policy_cache:
                            cache_policy(file_app, data)
                            if file_app == app:
                                base = data
                                break
                except Exception as exc:
                    logger.debug(f"[Dashboard] selfheal.yaml read error: {exc}")

    if base is None and not override:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No selfheal.yaml found for app '{app}'. "
                "Commit a selfheal.yaml to your repo root and push to register."
            ),
        )

    result = {**(base or {}), **override}
    result["_overrides_active"] = bool(override)
    result["_app"] = app
    return result


def apply_policy_override(app: str, override: PolicyOverride) -> dict[str, Any]:
    """Apply runtime override to policy."""
    if app not in _policy_overrides:
        _policy_overrides[app] = {}

    _policy_overrides[app][override.field] = override.value
    logger.info(
        f"[Dashboard] Policy override: app={app} field={override.field} value={override.value}"
    )
    return {
        "app": app,
        "overrides": _policy_overrides[app],
        "message": (
            f"Override applied: {override.field} = {override.value}. "
            "This is temporary — commit selfheal.yaml to make it permanent."
        ),
    }


def clear_policy_overrides(app: str) -> dict[str, str]:
    """Clear all runtime overrides for an app."""
    _policy_overrides.pop(app, None)
    return {"app": app, "message": "All overrides cleared."}
