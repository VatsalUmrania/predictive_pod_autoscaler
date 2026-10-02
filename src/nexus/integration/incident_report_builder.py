"""
Fact extraction and deterministic template generation for incident reports.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def build_report_facts(
    result: dict[str, Any], payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Deterministically extract the report facts from the terminal workflow state."""

    def _plain(obj: Any) -> dict[str, Any]:
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        return obj if isinstance(obj, dict) else {}

    payload = payload or {}
    diagnosis = _plain(result.get("diagnosis"))
    plan = _plain(result.get("plan"))
    verification = _plain(result.get("verification"))
    governance = _plain(result.get("governance"))
    target = _plain(result.get("target"))
    target_str = payload.get("target") or (
        f"{target.get('namespace', 'default')}/{target.get('name', 'unknown')}"
    )

    events = result.get("events") or []
    signal_type = (
        events[0].get("signal_type") if events and isinstance(events[0], dict) else None
    ) or payload.get("signal_type") or "unknown"

    timeline = [
        {
            "timestamp": e.get("timestamp", ""),
            "stage": e.get("stage", "unknown"),
            "action": e.get("action", ""),
            "details": e.get("details", ""),
        }
        for e in (result.get("audit_log") or [])
        if isinstance(e, dict)
    ]

    executed_actions = [
        {
            "tool": r.get("tool_name", "unknown"),
            "success": bool(r.get("success")),
            "duration_ms": r.get("duration_ms", 0.0),
            "error": r.get("error"),
        }
        for r in (result.get("execution_records") or [])
        if isinstance(r, dict)
    ]

    planned_actions = [
        f"{s.get('tool_name', 'action')}: {s.get('description', '')}".rstrip(": ")
        for s in plan.get("steps") or []
        if isinstance(s, dict)
    ]

    duration_s: float | None = None
    ts_list = [e["timestamp"] for e in timeline if e.get("timestamp")]
    last_ts = payload.get("resolved_at") or (ts_list[-1] if ts_list else None)
    if ts_list and last_ts:
        try:
            start = datetime.fromisoformat(ts_list[0].replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(last_ts).replace("Z", "+00:00"))
            duration_s = max(0.0, round((end - start).total_seconds(), 1))
        except ValueError:
            duration_s = None

    return {
        "incident_id": result.get("incident_id") or payload.get("incident_id"),
        "target": target_str,
        "outcome": "resolved" if result.get("resolved") else "escalated",
        "severity": result.get("severity") or "warning",
        "signal_type": signal_type,
        "timeline": timeline,
        "rca": {
            "failure_class": diagnosis.get("failure_class"),
            "root_cause": diagnosis.get("root_cause") or diagnosis.get("reasoning"),
            "confidence": diagnosis.get("confidence"),
            "suggested_fix": diagnosis.get("suggested_fix"),
        },
        "planned_actions": planned_actions,
        "executed_actions": executed_actions,
        "retries": len(result.get("remediation_history") or []),
        "rollback_executed": bool(result.get("rollback_executed")),
        "verification": {
            "healthy": verification.get("healthy"),
            "slo_restored": verification.get("slo_restored"),
            "details": verification.get("details"),
        },
        "governance_reasons": governance.get("reasons") or [],
        "reason": payload.get("reason") or result.get("error_message"),
        "duration_s": duration_s,
        "resolved_at": payload.get("resolved_at"),
    }


def template_report(facts: dict[str, Any]) -> dict[str, str]:
    """Deterministic fallback report — same sections, no LLM."""
    rca = facts["rca"]
    timeline_lines = [
        f"[{e['stage']}] {e['action']} — {e['details']}".rstrip(" —")
        for e in facts["timeline"]
    ] or ["No stage timeline recorded."]

    executed = facts["executed_actions"]
    action_lines = [
        f"{i + 1}. {a['tool']} — {'succeeded' if a['success'] else 'failed'}"
        + (f" in {a['duration_ms']:.0f}ms" if a["duration_ms"] else "")
        + (f" (error: {a['error']})" if a["error"] else "")
        for i, a in enumerate(executed)
    ] or [f"Planned: {p}" for p in facts["planned_actions"]] or [
        "No remediation executed."
    ]

    verif = facts["verification"]
    verification_text = verif["details"] or (
        "Target verified healthy."
        if verif.get("healthy")
        else "Target not verified healthy — escalated to operators."
    )

    dur = f" in {facts['duration_s']}s" if facts["duration_s"] is not None else ""
    if facts["outcome"] == "resolved":
        summary = (
            f"Incident on {facts['target']} ({facts['signal_type']}, severity "
            f"{facts['severity']}) was automatically resolved{dur}. "
        )
    else:
        summary = (
            f"Incident on {facts['target']} ({facts['signal_type']}, severity "
            f"{facts['severity']}) could not be resolved automatically and was "
            "escalated to human operators. "
        )
    summary += f"Root cause: {rca['root_cause'] or 'undetermined'}."

    return {
        "summary": summary,
        "timeline": "\n".join(timeline_lines),
        "root_cause": (
            f"Failure class: {rca['failure_class'] or 'unknown'} "
            f"(confidence {rca['confidence'] or 'n/a'}). "
            f"{rca['root_cause'] or 'Root cause not determined.'}"
        ),
        "impact": (
            f"{facts['target']} was degraded ({facts['severity']}); automated "
            f"response ran {facts['retries'] + 1} remediation attempt(s){dur}"
            + (", actions were rolled back" if facts["rollback_executed"] else "")
            + "."
        ),
        "remediation_actions": "\n".join(action_lines),
        "verification": verification_text,
        "recommendations": rca["suggested_fix"]
        or (
            "Manual review required — remediation did not restore health."
            if facts["outcome"] != "resolved"
            else "No further action required."
        ),
    }
