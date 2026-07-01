"""
Phase 2 — Monitoring: CloudWatch Alarms
=========================================
Describes alarm state and history. Used to understand alarm context.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AlarmInfo:
    """Current state of a CloudWatch alarm."""
    name: str
    state: str              # ALARM | OK | INSUFFICIENT_DATA
    reason: str
    namespace: str
    metric_name: str
    threshold: float
    dimensions: dict[str, str] = field(default_factory=dict)
    last_updated: datetime | None = None

    @property
    def is_alarming(self) -> bool:
        return self.state == "ALARM"


def describe_alarm(cw, alarm_name: str) -> AlarmInfo | None:
    """Fetch current state of a single CloudWatch alarm. Returns None if not found."""
    try:
        resp = cw.describe_alarms(AlarmNames=[alarm_name])
        alarms = resp.get("MetricAlarms", [])
        if not alarms:
            return None
        a = alarms[0]
        dims = {d["Name"]: d["Value"] for d in a.get("Dimensions", [])}
        ts_raw = a.get("StateUpdatedTimestamp")
        last_updated = ts_raw if isinstance(ts_raw, datetime) else None
        return AlarmInfo(
            name=a["AlarmName"],
            state=a.get("StateValue", "UNKNOWN"),
            reason=a.get("StateReason", "")[:500],
            namespace=a.get("Namespace", ""),
            metric_name=a.get("MetricName", ""),
            threshold=float(a.get("Threshold", 0)),
            dimensions=dims,
            last_updated=last_updated,
        )
    except Exception as exc:
        logger.debug(f"[Alarms] describe_alarm({alarm_name}): {exc}")
        return None


def list_alarming_alarms(cw, prefix: str = "") -> list[AlarmInfo]:
    """
    Return all alarms currently in ALARM state.
    Optionally filter by name prefix (e.g. "nexus-ai-agent").
    """
    alarms: list[AlarmInfo] = []
    try:
        kwargs: dict[str, Any] = {"StateValue": "ALARM"}
        if prefix:
            kwargs["AlarmNamePrefix"] = prefix
        resp = cw.describe_alarms(**kwargs)
        for a in resp.get("MetricAlarms", []):
            dims = {d["Name"]: d["Value"] for d in a.get("Dimensions", [])}
            alarms.append(AlarmInfo(
                name=a["AlarmName"],
                state="ALARM",
                reason=a.get("StateReason", "")[:500],
                namespace=a.get("Namespace", ""),
                metric_name=a.get("MetricName", ""),
                threshold=float(a.get("Threshold", 0)),
                dimensions=dims,
            ))
    except Exception as exc:
        logger.warning(f"[Alarms] list_alarming_alarms: {exc}")
    return alarms


def get_alarm_history(cw, alarm_name: str, hours: int = 24) -> list[dict]:
    """
    Return alarm state change history for the past N hours.
    Useful for understanding if this is a new or recurring issue.
    """
    start = datetime.now(timezone.utc) - timedelta(hours=hours)
    history = []
    try:
        resp = cw.describe_alarm_history(
            AlarmName=alarm_name,
            HistoryItemType="StateUpdate",
            StartDate=start,
        )
        for h in resp.get("AlarmHistoryItems", []):
            history.append({
                "timestamp": str(h.get("Timestamp", "")),
                "summary": h.get("HistorySummary", "")[:200],
            })
    except Exception as exc:
        logger.debug(f"[Alarms] get_alarm_history({alarm_name}): {exc}")
    return history
