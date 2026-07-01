"""
Phase 2 — Monitoring: AWS CloudTrail
=======================================
Queries CloudTrail for recent API calls related to a resource.
Useful for detecting recent deployments, permission changes, or config drift.

Common use: "Did someone update this Lambda in the last 30 minutes?"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


@dataclass
class TrailEvent:
    """A single CloudTrail event."""
    event_id: str
    event_name: str          # e.g. "UpdateFunctionConfiguration"
    event_time: datetime
    user: str                # IAM user or role
    source_ip: str
    resource_name: str
    error_code: str | None   # None = success


@dataclass
class TrailSummary:
    """CloudTrail events for a resource over a time window."""
    resource_name: str
    window_hours: int
    events: list[TrailEvent] = field(default_factory=list)
    recent_deployments: list[TrailEvent] = field(default_factory=list)
    permission_changes: list[TrailEvent] = field(default_factory=list)

    @property
    def was_recently_deployed(self) -> bool:
        return len(self.recent_deployments) > 0


# Lambda deployment events to watch
LAMBDA_DEPLOY_EVENTS = {
    "UpdateFunctionCode",
    "UpdateFunctionConfiguration",
    "PublishVersion",
    "UpdateAlias",
    "CreateFunction20150331",
}

LAMBDA_PERMISSION_EVENTS = {
    "AddPermission",
    "RemovePermission",
    "PutFunctionConcurrency",
}


def get_lambda_trail(
    ct_client,
    function_name: str,
    hours: int = 2,
) -> TrailSummary:
    """
    Fetch recent CloudTrail events for a Lambda function.
    Highlights recent deployments and permission changes.
    """
    start = datetime.now(timezone.utc) - timedelta(hours=hours)
    events: list[TrailEvent] = []

    try:
        resp = ct_client.lookup_events(
            LookupAttributes=[{
                "AttributeKey": "ResourceName",
                "AttributeValue": function_name,
            }],
            StartTime=start,
            MaxResults=50,
        )

        for raw in resp.get("Events", []):
            ev = _parse_event(raw)
            if ev:
                events.append(ev)

    except Exception as exc:
        logger.debug(f"[CloudTrail] get_lambda_trail({function_name}): {exc}")

    deploy_events = [e for e in events if e.event_name in LAMBDA_DEPLOY_EVENTS]
    perm_events = [e for e in events if e.event_name in LAMBDA_PERMISSION_EVENTS]

    return TrailSummary(
        resource_name=function_name,
        window_hours=hours,
        events=events,
        recent_deployments=deploy_events,
        permission_changes=perm_events,
    )


def get_iam_changes(ct_client, hours: int = 24) -> list[TrailEvent]:
    """Return recent IAM policy/role changes (could affect Lambda permissions)."""
    start = datetime.now(timezone.utc) - timedelta(hours=hours)
    events: list[TrailEvent] = []
    iam_events = {
        "AttachRolePolicy", "DetachRolePolicy", "PutRolePolicy",
        "DeleteRolePolicy", "CreatePolicy", "DeletePolicy",
    }
    try:
        resp = ct_client.lookup_events(
            LookupAttributes=[{"AttributeKey": "EventSource", "AttributeValue": "iam.amazonaws.com"}],
            StartTime=start,
            MaxResults=20,
        )
        for raw in resp.get("Events", []):
            ev = _parse_event(raw)
            if ev and ev.event_name in iam_events:
                events.append(ev)
    except Exception as exc:
        logger.debug(f"[CloudTrail] get_iam_changes: {exc}")
    return events


# ── Internal ──────────────────────────────────────────────────────────────────

def _parse_event(raw: dict) -> TrailEvent | None:
    try:
        resources = raw.get("Resources", [])
        resource_name = resources[0].get("ResourceName", "") if resources else ""
        user_identity = raw.get("Username", "") or raw.get("CloudTrailEvent", "")[:50]
        return TrailEvent(
            event_id=raw.get("EventId", ""),
            event_name=raw.get("EventName", ""),
            event_time=raw.get("EventTime", datetime.now(timezone.utc)),
            user=user_identity,
            source_ip=raw.get("SourceIPAddress", ""),
            resource_name=resource_name,
            error_code=raw.get("ErrorCode"),
        )
    except Exception:
        return None
