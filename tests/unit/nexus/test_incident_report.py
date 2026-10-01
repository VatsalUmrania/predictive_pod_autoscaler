"""Unit tests for post-incident report generation (LLM narrative + template fallback).

Covers:
  - build_report_facts() deterministic extraction from terminal workflow state
  - generate_post_incident_report() with an LLM that returns valid section JSON
  - generate_post_incident_report() template fallback when the LLM fails/unparsable
  - Partial LLM output: missing sections filled from the template
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.integration.incident_report import (
    SECTIONS,
    build_report_facts,
    generate_post_incident_report,
)

_LLM_SECTIONS = {
    "summary": "Auto-resolved crash loop caused by a bad environment variable.",
    "timeline": "10:00 alert ingested; 10:00:05 remediation verified healthy.",
    "root_cause": "Invalid env var crashed the container on boot.",
    "impact": "Checkout degraded for roughly 5 seconds.",
    "remediation_actions": "Restarted the deployment.",
    "verification": "Ready 3/3, zero 5xx.",
    "recommendations": "Pin the env var in the deployment manifest.",
}


def _terminal_result() -> dict:
    return {
        "incident_id": "inc-rpt-1",
        "resolved": True,
        "target": {"name": "checkout-service", "namespace": "ecommerce"},
        "severity": "critical",
        "events": [{"signal_type": "pod_restart_breach"}],
        "diagnosis": {
            "failure_class": "CrashLoopBackOff",
            "root_cause": "Bad env var caused startup crash",
            "confidence": 0.91,
            "suggested_fix": "Pin correct env var in the deployment manifest",
        },
        "plan": {"steps": [{"tool_name": "k8s_restart_deployment", "description": "Restart deployment"}]},
        "execution_records": [
            {"tool_name": "k8s_restart_deployment", "success": True, "duration_ms": 540.0, "error": None}
        ],
        "remediation_history": [],
        "verification": {"healthy": True, "slo_restored": True, "details": "Ready 3/3, 0% 5xx"},
        "audit_log": [
            {"timestamp": "2026-10-01T10:00:00+00:00", "stage": "triage", "action": "ingested", "details": "1 event"},
            {
                "timestamp": "2026-10-01T10:00:05+00:00",
                "stage": "learn",
                "action": "incident_resolved_and_indexed",
                "details": "healed",
            },
        ],
    }


class _FakeLLM:
    """Minimal stand-in for a LangChain chat model returning fixed content."""

    def __init__(self, content: str):
        self._content = content

    async def ainvoke(self, messages, **kwargs):
        m = MagicMock()
        m.content = self._content
        return m


def _mock_db_client() -> MagicMock:
    """PostgresClient-shaped mock: acquire() yields a conn with AsyncMock execute."""
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    client = MagicMock()
    acm = MagicMock()
    acm.__aenter__ = AsyncMock(return_value=conn)
    acm.__aexit__ = AsyncMock(return_value=False)
    client.acquire = MagicMock(return_value=acm)
    return client


def test_build_report_facts_extracts_evidence():
    facts = build_report_facts(_terminal_result(), payload={"outcome": "success"})

    assert facts["incident_id"] == "inc-rpt-1"
    assert facts["target"] == "ecommerce/checkout-service"
    assert facts["outcome"] == "resolved"
    assert facts["signal_type"] == "pod_restart_breach"
    assert len(facts["timeline"]) == 2
    assert facts["timeline"][0]["stage"] == "triage"
    assert facts["duration_s"] == 5.0
    assert facts["executed_actions"][0]["tool"] == "k8s_restart_deployment"
    assert facts["rca"]["root_cause"] == "Bad env var caused startup crash"
    assert facts["verification"]["details"] == "Ready 3/3, 0% 5xx"


@pytest.mark.asyncio
async def test_llm_narrative_used_and_persisted():
    """LLM returns all sections → generated_by=llm, report stored via SQL upsert."""
    db = _mock_db_client()
    with patch(
        "nexus.integration.incident_report.get_llm",
        return_value=_FakeLLM(json.dumps(_LLM_SECTIONS)),
    ):
        report = await generate_post_incident_report(_terminal_result(), client=db)

    assert report["generated_by"] == "llm"
    assert report["report"]["summary"] == _LLM_SECTIONS["summary"]
    assert report["report"]["root_cause"] == _LLM_SECTIONS["root_cause"]
    # Evidence facts ride along for the dashboard
    assert report["facts"]["incident_id"] == "inc-rpt-1"

    db.acquire.return_value.__aenter__.return_value.execute.assert_awaited_once()
    sql, inc_id, target, outcome, report_json = (
        db.acquire.return_value.__aenter__.return_value.execute.await_args.args
    )
    assert "INSERT INTO post_incident_reports" in sql
    assert inc_id == "inc-rpt-1"
    assert json.loads(report_json)["generated_by"] == "llm"


@pytest.mark.asyncio
async def test_template_fallback_when_llm_fails():
    """LLM raises → template fills every section; report still exists and persists."""
    db = _mock_db_client()

    def _boom():
        raise RuntimeError("gemini down")

    with patch("nexus.integration.incident_report.get_llm", side_effect=_boom):
        report = await generate_post_incident_report(_terminal_result(), client=db)

    assert report["generated_by"] == "template"
    for section in SECTIONS:
        assert report["report"][section], f"template section '{section}' must be non-empty"
    assert "Bad env var caused startup crash" in report["report"]["root_cause"]
    assert "ecommerce/checkout-service" in report["report"]["summary"]
    assert "k8s_restart_deployment — succeeded" in report["report"]["remediation_actions"]
    db.acquire.return_value.__aenter__.return_value.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_partial_llm_output_filled_from_template():
    """LLM returns only some sections → missing ones come from the template."""
    partial = {"summary": "LLM summary only"}
    db = _mock_db_client()
    with patch(
        "nexus.integration.incident_report.get_llm",
        return_value=_FakeLLM(json.dumps(partial)),
    ):
        report = await generate_post_incident_report(_terminal_result(), client=db)

    assert report["generated_by"] == "llm"
    assert report["report"]["summary"] == "LLM summary only"
    # root_cause was missing from the LLM output → template filled it
    assert "Bad env var caused startup crash" in report["report"]["root_cause"]


# ── HTML report page ─────────────────────────────────────────────────────────

def test_render_report_html_escapes_content():
    """Report page escapes LLM/incident text (trust boundary) and renders sections."""
    from nexus.integration.incident_report import render_report_html

    report = {
        "incident_id": "inc-xss",
        "target": "default/pod<script>alert(1)</script>",
        "outcome": "resolved",
        "generated_at": "2026-10-01T10:00:05+00:00",
        "generated_by": "llm",
        "report": {
            "summary": "Fixed <b>bad</b> config",
            "timeline": "",
            "root_cause": "Bad env var",
            "impact": "",
            "remediation_actions": "Restarted deployment",
            "verification": "",
            "recommendations": "",
        },
        "facts": {"timeline": [], "executed_actions": [{"tool": "k8s_restart"}]},
    }

    page = render_report_html(report)

    import html as html_mod

    assert "<script>" not in page
    assert html_mod.escape("<script>") in page  # target contained a script tag → escaped
    assert html_mod.escape("<b>bad</b>") in page  # LLM summary escaped too
    assert "Root Cause" in page
    assert "Evidence Timeline" not in page  # no raw timeline entries → omitted
    assert "Impact" not in page  # empty section omitted
    assert 'class="outcome resolved"' in page


def test_render_report_missing_page():
    from nexus.integration.incident_report import render_report_missing_html

    page = render_report_missing_html("inc-404")
    assert "No report for this incident yet" in page
    assert "inc-404" in page


@pytest.mark.asyncio
async def test_report_endpoint_content_negotiation():
    """Browsers get the readable HTML page; API clients get raw JSON."""
    from httpx import ASGITransport, AsyncClient

    from nexus.observability.status_api import app

    stored = {
        "incident_id": "inc-nego",
        "target": "default/api",
        "outcome": "resolved",
        "generated_at": "2026-10-01T10:00:05+00:00",
        "report": {"summary": "All good"},
        "facts": {},
    }

    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=stored)
    db = MagicMock()
    acm = MagicMock()
    acm.__aenter__ = AsyncMock(return_value=conn)
    acm.__aexit__ = AsyncMock(return_value=False)
    db.acquire = MagicMock(return_value=acm)

    with patch("nexus.integration.incident_report.get_database_client", return_value=db):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Browser: Accept: text/html
            res_html = await client.get(
                "/developer/incidents/inc-nego/report",
                headers={"accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
            )
            assert res_html.status_code == 200
            assert "text/html" in res_html.headers["content-type"]
            assert "All good" in res_html.text
            assert "NEXUS Post-Incident Report" in res_html.text

            # API client: no text/html in Accept
            res_json = await client.get(
                "/developer/incidents/inc-nego/report",
                headers={"accept": "application/json"},
            )
            assert res_json.status_code == 200
            assert res_json.json()["report"]["summary"] == "All good"

            # Missing report, browser → styled 404 page
            conn.fetchrow = AsyncMock(return_value=None)
            res_missing = await client.get(
                "/developer/incidents/inc-none/report",
                headers={"accept": "text/html"},
            )
            assert res_missing.status_code == 404
            assert "No report for this incident yet" in res_missing.text
