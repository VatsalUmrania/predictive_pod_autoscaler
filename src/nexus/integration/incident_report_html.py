"""
HTML rendering functions for NEXUS Post-Incident Reports.
"""

from __future__ import annotations

import html
import json
from typing import Any

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
