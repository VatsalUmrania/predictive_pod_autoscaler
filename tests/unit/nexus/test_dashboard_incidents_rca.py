import pytest
from httpx import ASGITransport, AsyncClient

from nexus.integration.dashboard import _read_incidents, _write_incident
from nexus.observability.status_api import app


@pytest.mark.asyncio
async def test_serve_dashboard_html():
    """Verify that the developer dashboard HTML is served at root with aspects, accept, approve, and RCA."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/")
        assert res.status_code == 200
        assert "text/html" in res.headers["content-type"]
        html = res.text
        # Verify restored white theme palette and dark theme support
        assert "--bg: #f8fafc" in html
        assert "--text: #1e293b" in html
        assert "--bg: #07070f" in html
        assert "themeToggle" in html
        assert "toggleTheme" in html
        # Verify core aspects, navigation, and toolbar
        assert "Incident Feed" in html
        assert "aspect-toolbar" in html
        assert "Awaiting Approval" in html
        # Verify accept, approve, reject actions
        assert "btn-accept" in html
        assert "btn-approve" in html
        assert "btn-reject" in html
        assert "function accept(" in html
        assert "function approve(" in html
        assert "function reject(" in html
        # Verify RCA block
        assert "rca-box" in html
        assert "Root Cause Analysis (RCA)" in html


@pytest.mark.asyncio
async def test_dashboard_incidents_sqlite_rca_and_accept(tmp_path, monkeypatch):
    """Verify SQLite persistence of rca, accepted_by, accepted_at in dashboard.py."""
    db_file = str(tmp_path / "test_dashboard_incidents.db")
    monkeypatch.setattr("nexus.integration.dashboard._INCIDENT_DB", db_file)

    row = {
        "incident_id": "INC-TEST-001",
        "runbook_id": "runbook_high_error_rate_post_deploy_v1",
        "target": "checkout-service",
        "level": 2,
        "outcome": "pending",
        "description": "High error rate detected after deploy.",
        "confidence": 0.94,
        "timestamp": "2026-09-05T12:00:00",
        "rca": {
            "root_cause": "NPE in PaymentAdapter",
            "failure_class": "bad_deploy",
            "confidence": 0.94,
            "source": "gemini",
        },
        "accepted_by": "dev-oncall",
        "accepted_at": "2026-09-05T12:05:00",
    }

    _write_incident(row)
    read_back = _read_incidents(n=10, app=None)
    assert len(read_back) == 1
    item = read_back[0]
    assert item["incident_id"] == "INC-TEST-001"
    assert item["outcome"] == "pending"
    assert item["accepted_by"] == "dev-oncall"
    assert item["accepted_at"] == "2026-09-05T12:05:00"
    assert isinstance(item["rca"], dict)
    assert item["rca"]["root_cause"] == "NPE in PaymentAdapter"
    assert item["rca"]["source"] == "gemini"
