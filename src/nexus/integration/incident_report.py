"""
NEXUS Post-Incident Reports
===========================
Resolve.ai-style postmortem drafts generated when an incident reaches a
terminal state (resolved or escalated). The LLM writes the narrative from
the validated workflow state; a deterministic template fills the same
sections when the LLM is unavailable — a report ALWAYS exists.

Sections: summary, timeline, root_cause, impact, remediation_actions,
verification, recommendations. Stored as JSONB in post_incident_reports
together with the raw evidence facts.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

from nexus.db.postgres import get_database_client
from nexus.graph.llm import get_llm

logger = logging.getLogger(__name__)

SECTIONS = (
    "summary",
    "timeline",
    "root_cause",
    "impact",
    "remediation_actions",
    "verification",
    "recommendations",
)

_SYSTEM_PROMPT = (
    "You are NEXUS, an autonomous AI-SRE. Write a concise, factual post-incident "
    "report for the incident described in the user message. Respond ONLY with a "
    "JSON object with exactly these string-valued keys: "
    + ", ".join(SECTIONS)
    + ". Use only the facts provided — never invent metrics, logs, or causes. "
    "timeline is a chronological narrative; recommendations are actionable "
    "preventive measures."
)


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

    # Wall-clock duration across the recorded stages
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


def _template_report(facts: dict[str, Any]) -> dict[str, str]:
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
        "Target verified healthy." if verif.get("healthy")
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


async def _llm_report(facts: dict[str, Any]) -> dict[str, str] | None:
    """Ask the LLM to write the narrative report; None on any failure."""
    timeout_s = float(os.getenv("NEXUS_REPORT_LLM_TIMEOUT_S", "60"))
    try:
        llm = get_llm()
        resp = await asyncio.wait_for(
            llm.ainvoke([("system", _SYSTEM_PROMPT), ("human", json.dumps(facts))]),
            timeout=timeout_s,
        )
        text = str(getattr(resp, "content", resp)).strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        data = json.loads(text)
        if not isinstance(data, dict):
            return None
        return {s: str(data.get(s, "")).strip() for s in SECTIONS}
    except Exception as exc:
        logger.warning("[IncidentReport] LLM narrative failed: %s", exc)
        return None


async def generate_post_incident_report(
    result: dict[str, Any],
    payload: dict[str, Any] | None = None,
    client: Any = None,
) -> dict[str, Any]:
    """Build the post-incident report (LLM narrative, template fallback) and persist it."""
    facts = build_report_facts(result, payload)
    template = _template_report(facts)
    sections = dict(template)
    generated_by = "template"

    llm_sections = await _llm_report(facts)
    if llm_sections and any(llm_sections.get(s) for s in SECTIONS):
        generated_by = "llm"
        sections = {s: (llm_sections.get(s) or template[s]) for s in SECTIONS}

    report = {
        "incident_id": facts["incident_id"],
        "target": facts["target"],
        "outcome": facts["outcome"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": generated_by,
        "report": sections,
        "facts": facts,
    }
    await _store_report(report, client=client)
    return report


async def _store_report(report: dict[str, Any], client: Any = None) -> None:
    """Persist the report JSONB (idempotent — latest report wins on re-fire)."""
    try:
        db = client or await get_database_client()
        async with db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO post_incident_reports (incident_id, target, outcome, report)
                VALUES ($1, $2, $3, $4::jsonb)
                ON CONFLICT (incident_id) DO UPDATE SET
                    target = EXCLUDED.target,
                    outcome = EXCLUDED.outcome,
                    report = EXCLUDED.report,
                    generated_at = NOW()
                """,
                str(report.get("incident_id")),
                report.get("target"),
                report.get("outcome"),
                json.dumps(report),
            )
    except Exception as exc:
        logger.warning("[IncidentReport] Report persistence failed: %s", exc)


async def get_incident_report(incident_id: str, client: Any = None) -> dict[str, Any] | None:
    """Load one stored report (used by the dashboard API)."""
    try:
        db = client or await get_database_client()
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT incident_id, target, outcome, generated_at, report
                FROM post_incident_reports WHERE incident_id = $1
                """,
                incident_id,
            )
        if not row:
            return None
        d = dict(row)
        if isinstance(d.get("report"), str):
            d["report"] = json.loads(d["report"])
        return d
    except Exception as exc:
        logger.warning("[IncidentReport] Report load failed: %s", exc)
        return None


def report_url(incident_id: str) -> str:
    """Absolute URL to the dashboard report page, for Slack cards."""
    base = os.getenv("NEXUS_DASHBOARD_BASE_URL", "http://localhost:8080").rstrip("/")
    return f"{base}/developer/incidents/{incident_id}/report"


# ── HTML rendering (standalone report page served to browsers) ────────────────
# Served when an operator opens the Slack "Open full report" link. Reuses the
# developer dashboard's Geist/token palette; zero JS, dark mode via
# prefers-color-scheme. All interpolated text is escaped: report content is
# LLM output mixed with incident data, so this is a trust boundary.

_REPORT_FONT_LINK = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700'
    '&family=Geist+Mono:wght@400;500;600&display=swap" rel="stylesheet">'
)

_REPORT_CSS = """
:root{--bg:#f8fafc;--surface:#ffffff;--s2:#f1f5f9;--border:rgba(15,23,42,.08);
--text:#1e293b;--text-muted:#64748b;--text-bright:#0f172a;--accent:#2563eb;
--accent-muted:rgba(37,99,235,.08);--green:#059669;--green-bg:rgba(5,150,105,.08);
--green-border:rgba(5,150,105,.25);--red:#dc2626;--red-bg:rgba(220,38,38,.08);
--red-border:rgba(220,38,38,.25);--r:12px;--r-sm:8px}
@media(prefers-color-scheme:dark){:root{--bg:#07070f;--surface:#0f0f1c;--s2:#161626;
--border:rgba(255,255,255,.08);--text:#e0e0f0;--text-muted:#8c8ca8;--text-bright:#fff;
--accent:#8ab4f8;--accent-muted:rgba(138,180,248,.15);--green:#00ff88;
--green-bg:rgba(0,255,136,.08);--green-border:rgba(0,255,136,.25);--red:#ff4d6a;
--red-bg:rgba(255,77,106,.08);--red-border:rgba(255,77,106,.25)}}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Geist',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:var(--bg);color:var(--text);line-height:1.55;-webkit-font-smoothing:antialiased;
min-height:100vh;padding:48px 20px}
main{max-width:860px;margin:0 auto}
.eyebrow{font-size:12px;font-weight:600;letter-spacing:.08em;color:var(--accent);
margin-bottom:10px}
h1{font-size:32px;font-weight:700;letter-spacing:-.02em;color:var(--text-bright);
margin-bottom:14px;line-height:1.15}
.meta{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;font-size:13px;
color:var(--text-muted);margin-bottom:36px}
.meta code{font-family:'Geist Mono',ui-monospace,monospace;font-size:12px;
background:var(--s2);padding:2px 7px;border-radius:6px;color:var(--text)}
.outcome{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600;
padding:4px 12px;border-radius:9999px;border:1px solid}
.outcome.resolved{color:var(--green);background:var(--green-bg);border-color:var(--green-border)}
.outcome.escalated{color:var(--red);background:var(--red-bg);border-color:var(--red-border)}
.summary{background:var(--accent-muted);border:1px solid var(--border);
border-left:3px solid var(--accent);border-radius:var(--r);padding:18px 20px;
font-size:15.5px;font-weight:500;color:var(--text-bright);margin-bottom:36px;white-space:pre-wrap}
section{margin-bottom:34px}
section h2{font-size:15px;font-weight:600;color:var(--text-bright);margin-bottom:10px}
section .body{font-size:14.5px;color:var(--text);white-space:pre-wrap}
.tl{list-style:none;border-left:2px solid var(--border);margin-left:6px}
.tl li{position:relative;padding:0 0 18px 18px}
.tl li::before{content:'';position:absolute;left:-5px;top:6px;width:8px;height:8px;
border-radius:50%;background:var(--accent)}
.tl .stage{font-weight:600;color:var(--text-bright);font-size:13.5px}
.tl .ts{font-family:'Geist Mono',ui-monospace,monospace;font-size:11.5px;
color:var(--text-muted);margin-right:8px}
.tl .det{font-size:13px;color:var(--text-muted)}
details{border:1px solid var(--border);border-radius:var(--r);background:var(--surface);
padding:14px 18px;margin-top:8px}
details summary{cursor:pointer;font-size:13.5px;font-weight:600;color:var(--text-muted)}
details pre{font-family:'Geist Mono',ui-monospace,monospace;font-size:11.5px;
background:var(--s2);border-radius:var(--r-sm);padding:14px;overflow:auto;
margin-top:12px;color:var(--text);white-space:normal}
footer{margin-top:44px;padding-top:18px;border-top:1px solid var(--border);
font-size:13px;color:var(--text-muted)}
footer a{color:var(--accent);text-decoration:none;font-weight:600}
footer a:hover{text-decoration:underline}
.empty-page{text-align:center;padding:80px 0}
.empty-page h1{font-size:22px}
.empty-page p{color:var(--text-muted);font-size:14.5px;margin:10px 0 26px}
"""


def _section_html(title: str, body: str) -> str:
    """One prose section; caller skips empty bodies."""
    return (
        f'<section><h2>{html.escape(title)}</h2>'
        f'<div class="body">{html.escape(body)}</div></section>'
    )


def _report_sections_html(sections: dict[str, Any]) -> str:
    """Render the narrative prose sections; skip empty ones."""
    titles = {
        "timeline": "Timeline",
        "root_cause": "Root Cause",
        "impact": "Impact",
        "remediation_actions": "Remediation Actions",
        "verification": "Verification",
        "recommendations": "Recommendations",
    }
    out = []
    for key, title in titles.items():
        text = str(sections.get(key) or "").strip()
        if not text:
            continue
        out.append(_section_html(title, text))
    return "\n".join(out)


def _timeline_html(facts: dict[str, Any]) -> str:
    """Vertical stage timeline built from the raw audit evidence."""
    entries = facts.get("timeline") or []
    if not entries:
        return ""
    items = []
    for e in entries:
        items.append(
            f'<li><span class="ts">{html.escape(str(e.get("timestamp") or ""))}</span>'
            f'<span class="stage">{html.escape(str(e.get("stage") or "unknown"))}</span>'
            f'<div class="det">{html.escape(str(e.get("action") or ""))}'
            f"{(' — ' + html.escape(str(e.get('details') or ''))) if e.get('details') else ''}</div></li>"
        )
    return (
        '<section><h2>Evidence Timeline</h2><ul class="tl">'
        + "\n".join(items)
        + "</ul></section>"
    )


def render_report_html(report: dict[str, Any]) -> str:
    """Standalone, JS-free report page for browsers (Slack link target)."""
    sections = report.get("report") or {}
    facts = report.get("facts") or {}
    outcome = str(report.get("outcome") or "resolved")
    out_cls = "resolved" if outcome == "resolved" else "escalated"
    out_label = "Resolved" if outcome == "resolved" else "Escalated"

    summary = str(sections.get("summary") or "").strip()
    summary_html = (
        f'<div class="summary">{html.escape(summary)}</div>' if summary else ""
    )

    facts_json = html.escape(json.dumps(facts, indent=2, default=str))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NEXUS Post-Incident Report - {html.escape(str(report.get("target") or report.get("incident_id") or ""))}</title>
{_REPORT_FONT_LINK}
<style>{_REPORT_CSS}</style>
</head>
<body>
<main>
  <div class="eyebrow">NEXUS Post-Incident Report</div>
  <h1>{html.escape(str(report.get("target") or "Unknown target"))}</h1>
  <div class="meta">
    <span class="outcome {out_cls}">{out_label}</span>
    <span>ID <code>{html.escape(str(report.get("incident_id") or ""))}</code></span>
    <span>Generated {html.escape(str(report.get("generated_at") or "")[:19].replace("T", " "))} UTC</span>
    <span>by {html.escape(str(report.get("generated_by") or "template"))} narrative</span>
  </div>
  {summary_html}
  {_report_sections_html(sections)}
  {_timeline_html(facts)}
  <details>
    <summary>Evidence facts (raw)</summary>
    <pre>{facts_json}</pre>
  </details>
  <footer>
    <a href="/">Back to the NEXUS dashboard</a>
  </footer>
</main>
</body>
</html>"""


def render_report_missing_html(incident_id: str) -> str:
    """Styled 404 for incidents that predate report generation."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Report not found - NEXUS</title>
{_REPORT_FONT_LINK}
<style>{_REPORT_CSS}</style>
</head>
<body>
<main class="empty-page">
  <div class="eyebrow">NEXUS Post-Incident Report</div>
  <h1>No report for this incident yet</h1>
  <p>Incident <code style="font-family:'Geist Mono',monospace">{html.escape(incident_id)}</code>
  either predates report generation or never reached a terminal state.</p>
  <p><a href="/">Back to the NEXUS dashboard</a></p>
</main>
</body>
</html>"""
