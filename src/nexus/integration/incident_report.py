"""
NEXUS Post-Incident Reports
===========================
Resolve.ai-style postmortem drafts generated when an incident reaches a terminal state.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

from nexus.db.postgres import get_database_client
from nexus.graph.llm import get_llm
from nexus.integration.incident_report_builder import (
    build_report_facts,
    template_report,
)
from nexus.integration.incident_report_html import (
    render_report_html,
    render_report_missing_html,
)

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

_template_report = template_report

__all__ = [
    "SECTIONS",
    "build_report_facts",
    "_template_report",
    "generate_post_incident_report",
    "get_incident_report",
    "report_url",
    "render_report_html",
    "render_report_missing_html",
    "get_llm",
    "get_database_client",
]


async def _llm_report(facts: dict[str, Any]) -> dict[str, str] | None:
    """Ask the LLM to write the narrative report; None on any failure."""
    if os.getenv("NEXUS_REPORT_USE_LLM", "true").lower() == "false":
        return None
    timeout_s = float(os.getenv("NEXUS_REPORT_LLM_TIMEOUT_S", "60"))
    try:
        llm = get_llm()
        resp = await asyncio.wait_for(
            llm.ainvoke(
                [("system", _SYSTEM_PROMPT), ("human", json.dumps(facts, default=str))]
            ),
            timeout=timeout_s,
        )
        text = str(getattr(resp, "content", resp)).strip()
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        candidate = m.group(1).strip() if m else text
        if not candidate.startswith("{"):
            brace_m = re.search(r"(\{.*\})", candidate, re.DOTALL)
            if brace_m:
                candidate = brace_m.group(1).strip()
        try:
            data = json.loads(candidate)
        except Exception:
            try:
                data = ast.literal_eval(candidate)
            except Exception:
                return None
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
    template = template_report(facts)
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


async def get_incident_report(
    incident_id: str, client: Any = None
) -> dict[str, Any] | None:
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
