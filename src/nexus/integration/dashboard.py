"""
NEXUS Developer Dashboard API
===============================
Three developer-facing views backed by existing NEXUS data stores.

Mounted at /developer/* on the Status API.

Endpoints:
    GET /developer/incidents?n=20&app=checkout-service
        Plain-English healing incident feed from AuditTrail.
        Each entry is a human-readable sentence: what happened, when, why.

    GET /developer/predictions?app=checkout-service
        Current traffic forecasts from Prescaler + DBTrafficCorrelator.
        Format: { endpoint, current_rps, predicted_rps, horizon_min, confidence, tables }

    GET /developer/policy/{app}
        Resolved selfheal.yaml policy (from NATS KV or in-memory cache).

    PUT /developer/policy/{app}
        Runtime override of a specific policy field without a git push.
        Useful for incident response: temporarily disable auto_rollback, etc.

These endpoints are designed for developer dashboards, not ops tooling.
The language used in incident descriptions is intentionally non-technical.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()

# ──────────────────────────────────────────────────────────────────────────────
# In-memory policy override store (runtime patches on top of selfheal.yaml)
# ──────────────────────────────────────────────────────────────────────────────

_policy_overrides: Dict[str, Dict[str, Any]] = {}
_policy_cache:     Dict[str, Dict[str, Any]] = {}   # app_name → resolved SelfhealConfig dict


def cache_policy(app_name: str, policy_dict: Dict[str, Any]) -> None:
    """Called by GitAgent when a new selfheal.yaml is processed."""
    _policy_cache[app_name] = policy_dict
    logger.info(f"[Dashboard] Policy cached for app='{app_name}'")


# ── In-memory incident store (populated by demo orchestrator or SDK events) ───
_incident_store: List[Dict[str, Any]] = []


# ──────────────────────────────────────────────────────────────────────────────
# Plain-English incident description builder
# ──────────────────────────────────────────────────────────────────────────────

_RUNBOOK_DESCRIPTIONS = {
    "runbook_pod_crashloop_v1":
        "Restarted {target} which was crash-looping",
    "runbook_high_error_rate_post_deploy_v1":
        "Rolled back deployment {target} — error rate spiked after a code push",
    "runbook_missing_env_key_v1":
        "Blocked deployment of {target} — required environment variables were missing",
    "runbook_dns_resolution_failure_v1":
        "Triggered DNS refresh for {target} — DNS resolution was failing",
    "runbook_db_connection_exhaustion_v1":
        "Reset the database connection pool for {target} — connections were exhausted",
}

_OUTCOME_SUFFIX = {
    "success":     "✅ Successfully resolved.",
    "failed":      "❌ Healing attempt failed — may need manual review.",
    "rolled_back": "↩️  Action was rolled back (post-check failed).",
    "pending":     "⏳ Action is in progress.",
}


def _make_plain_english(row: Dict[str, Any]) -> str:
    """Turn an AuditTrail row into a plain-English incident description."""
    runbook_id = row.get("runbook_id", "")
    target     = row.get("target") or row.get("resource_name") or "the service"
    outcome    = row.get("execution_outcome", "pending")
    ts         = (row.get("timestamp") or "")[:19].replace("T", " ")

    template = _RUNBOOK_DESCRIPTIONS.get(
        runbook_id,
        f"Performed healing action '{runbook_id}' on {{target}}"
    )
    action  = template.format(target=target)
    suffix  = _OUTCOME_SUFFIX.get(outcome, "")

    return f"At {ts} UTC — {action}. {suffix}".strip()


# ──────────────────────────────────────────────────────────────────────────────
# Context proxy (avoids circular import with status_api)
# ──────────────────────────────────────────────────────────────────────────────

def _context():
    from nexus.observability.status_api import context
    return context


# ──────────────────────────────────────────────────────────────────────────────
# Incidents
# ──────────────────────────────────────────────────────────────────────────────

@router.get("/developer/incidents", tags=["developer"])
async def developer_incidents(
    n:   int           = 20,
    app: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Plain-English healing incident feed.

    Each entry includes a human-readable description of what NEXUS did,
    when it happened, and whether it succeeded — suitable for display in
    a developer dashboard without requiring knowledge of runbook internals.

    Optionally filter by app name (requires 'target' field in AuditTrail to
    match the app name).
    """
    ctx = _context()
    if ctx.audit_trail is None:
        return []

    rows = await ctx.audit_trail.query_recent(limit=max(n * 3, 60))  # fetch extra for filtering

    results = []
    for row in rows:
        # Optional app filter
        if app:
            target = (row.get("target") or row.get("resource_name") or "").lower()
            if app.lower() not in target:
                continue

        results.append({
            "timestamp":    (row.get("timestamp") or "")[:19],
            "runbook_id":   row.get("runbook_id"),
            "target":       row.get("target") or row.get("resource_name"),
            "level":        row.get("healing_level"),
            "outcome":      row.get("execution_outcome", "pending"),
            "description":  _make_plain_english(row),
            "incident_id":  row.get("incident_id") or row.get("correlation_id"),
        })

        if len(results) >= n:
            break

    # Also merge incidents pushed directly via POST /developer/incidents
    # (used by demo orchestrator when running without full NEXUS core AuditTrail)
    for entry in reversed(_incident_store):
        if app:
            target = (entry.get("target") or "").lower()
            if app.lower() not in target:
                continue
        results.append(entry)
        if len(results) >= n:
            break

    # Sort all by timestamp descending
    results.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return results[:n]



@router.post("/developer/incidents", tags=["developer"])
async def developer_post_incident(payload: Dict[str, Any]) -> Dict[str, str]:
    """
    Accept an incident directly from the demo orchestrator or SDK.
    Used when the full AuditTrail chain is not available (e.g. local dev mode).
    """
    import uuid
    from datetime import datetime, timezone
    payload.setdefault("incident_id", str(uuid.uuid4())[:8])
    payload.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    _incident_store.append(payload)
    # Keep at most 200 incidents
    if len(_incident_store) > 200:
        _incident_store.pop(0)
    return {"status": "ok", "incident_id": payload["incident_id"]}


# ──────────────────────────────────────────────────────────────────────────────
# Predictions
# ──────────────────────────────────────────────────────────────────────────────

@router.get("/developer/predictions", tags=["developer"])
def developer_predictions(app: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Current traffic predictions from the Prescaler.

    Shows what NEXUS is predicting for the next N minutes, including:
    - Which endpoint is expected to spike
    - How much traffic is predicted (current vs predicted RPS)
    - Which DB tables are driving the prediction
    - Whether NEXUS is pre-scaling and in which mode

    The language mirrors the Integration.md example:
    "Based on the spike in reads to `orders` in the last 12 minutes,
     /api/orders is predicted to receive 3.2x normal traffic in 14 minutes."
    """
    ctx = _context()
    if ctx.prescaler is None:
        return []

    predictions = []
    for d in reversed(ctx.prescaler._all_decisions[-20:]):
        # Filter by app name if requested (match against deployment_name)
        if app and app.lower() not in (d.deployment_name or "").lower():
            continue

        multiplier = (d.predicted_rps / d.current_rps) if d.current_rps > 0 else 1.0

        predictions.append({
            "deployment":      d.deployment_name,
            "namespace":       d.namespace,
            "current_rps":     round(d.current_rps, 1),
            "predicted_rps":   round(d.predicted_rps, 1),
            "multiplier":      round(multiplier, 2),
            "confidence":      round(d.confidence, 3),
            "mode":            ctx.prescaler.stats.get("mode", "shadow"),
            "outcome":         d.outcome or "pending",
            "decided_at":      d.decided_at,
            "description":     _make_prediction_sentence(d),
        })

    return predictions


def _make_prediction_sentence(d: Any) -> str:
    """Build a plain-English prediction sentence from a PrescaleDecision."""
    try:
        mult   = d.predicted_rps / d.current_rps if d.current_rps > 0 else 1.0
        tables = getattr(d, "trigger_tables", []) or []
        table_clause = (
            f" — driven by reads to {', '.join(f'`{t}`' for t in tables[:3])}"
            if tables else ""
        )
        return (
            f"{d.deployment_name} is predicted to receive "
            f"{mult:.1f}x normal traffic "
            f"({d.current_rps:.0f} → {d.predicted_rps:.0f} RPS)"
            f"{table_clause}."
        )
    except Exception:
        return "Traffic spike predicted."


# ──────────────────────────────────────────────────────────────────────────────
# Policy
# ──────────────────────────────────────────────────────────────────────────────

@router.get("/developer/policy/{app}", tags=["developer"])
def developer_get_policy(app: str) -> Dict[str, Any]:
    """
    Return the resolved selfheal.yaml policy for an app.
    Includes any runtime overrides applied via PUT.

    Falls back to reading from a mounted selfheal.yaml file at:
      $NEXUS_SELFHEAL_YAML_PATH  (env var override)
      /app/selfheal.yaml          (default — mounted via docker-compose volume)
    """
    import os
    from pathlib import Path

    base     = _policy_cache.get(app)
    override = _policy_overrides.get(app, {})

    # ── Filesystem fallback ──────────────────────────────────────────────────
    # Try to read from a mounted selfheal.yaml if cache is empty.
    # This means the policy is always available as long as the file is mounted,
    # without requiring the backend to POST the policy on startup.
    if base is None:
        _candidates = [
            os.environ.get("NEXUS_SELFHEAL_YAML_PATH", ""),
            "/app/selfheal.yaml",           # mounted by docker-compose
            "/data/selfheal.yaml",          # alternate mount point
        ]
        for _path_str in _candidates:
            if not _path_str:
                continue
            _p = Path(_path_str)
            if _p.exists():
                try:
                    import yaml as _yaml
                    _data = _yaml.safe_load(_p.read_text())
                    if isinstance(_data, dict):
                        # If app matches OR no app filter given, cache and use it
                        _file_app = _data.get("app", app)
                        if _file_app == app or app not in _policy_cache:
                            cache_policy(_file_app, _data)
                            if _file_app == app:
                                base = _data
                                break
                except Exception as _e:
                    logger.debug(f"[Dashboard] selfheal.yaml read error: {_e}")

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
    result["_app"]              = app
    return result


@router.post("/developer/policy/{app}", tags=["developer"])
def developer_upload_policy(app: str, policy: Dict[str, Any]) -> Dict[str, Any]:
    """
    Upload and cache a selfheal.yaml policy for an app.

    Called by the SDK on app startup — the app reads its selfheal.yaml from disk
    and POSTs the content here so the dashboard can display and edit it.

    This is the HTTP-safe alternative to calling cache_policy() directly
    (which only works in-process).
    """
    cache_policy(app, policy)
    return {"app": app, "message": "Policy uploaded and cached.", "fields": list(policy.keys())}


class PolicyOverride(BaseModel):
    field: str   # dot-path e.g. "healing_policy.auto_rollback"
    value: Any


@router.put("/developer/policy/{app}", tags=["developer"])
def developer_put_policy(app: str, override: PolicyOverride) -> Dict[str, Any]:
    """
    Apply a runtime override to an app's selfheal.yaml policy.

    Changes take effect immediately for new healing decisions.
    Overrides are NOT persisted — they reset on NEXUS restart.
    To make permanent changes, update selfheal.yaml in the repo.

    Example:
        PUT /developer/policy/checkout-service
        { "field": "healing_policy.auto_rollback", "value": false }
    """
    if app not in _policy_overrides:
        _policy_overrides[app] = {}

    _policy_overrides[app][override.field] = override.value
    logger.info(
        f"[Dashboard] Policy override: app={app} "
        f"field={override.field} value={override.value}"
    )
    return {
        "app":       app,
        "overrides": _policy_overrides[app],
        "message":   (
            f"Override applied: {override.field} = {override.value}. "
            "This is temporary — commit selfheal.yaml to make it permanent."
        ),
    }


@router.delete("/developer/policy/{app}/overrides", tags=["developer"])
def developer_clear_overrides(app: str) -> Dict[str, str]:
    """Clear all runtime overrides for an app, reverting to selfheal.yaml defaults."""
    _policy_overrides.pop(app, None)
    return {"app": app, "message": "All overrides cleared."}
