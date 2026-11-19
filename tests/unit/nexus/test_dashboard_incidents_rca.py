import pytest
from httpx import ASGITransport, AsyncClient

from nexus.services.api.status.status_api import app
from nexus.services.observability.dashboard.dashboard import (
    _read_incidents,
    _write_incident,
)


@pytest.mark.asyncio
async def test_serve_dashboard_html():
    """Verify that the developer dashboard HTML is served at root with SRE console UI, HITL approval, and RCA."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/")
        assert res.status_code == 200
        assert "text/html" in res.headers["content-type"]
        html = res.text
        # Verify theme palette and control plane branding
        assert "--background: oklch(0.0400 0.0050 270)" in html
        assert "NEXUS Autonomous SRE Control Plane" in html
        # Verify incident feed, filters, and HITL approval actions
        assert "LangGraph Incident Threads" in html
        assert "Awaiting Approval" in html
        assert "btn-approve" in html
        assert "btn-reject" in html
        assert "async function submitApprovalDecision(" in html
        # Verify ReAct investigator / RCA and OPA panels
        assert "ReAct Investigator (diagnose_node)" in html
        assert "Synthesized Root Cause" in html
        assert "Schema-Bound Plan &amp; OPA Policy" in html


@pytest.mark.asyncio
async def test_dashboard_incidents_postgres_rca_and_accept():
    """Verify PostgreSQL persistence of rca, accepted_by, accepted_at in dashboard.py."""
    from tests.unit.nexus.test_db_helpers import MockPostgresClient

    mock_client = MockPostgresClient()

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

    await _write_incident(row, client=mock_client)
    read_back = await _read_incidents(n=10, app=None, client=mock_client)
    assert len(read_back) == 1
    item = read_back[0]
    assert item["incident_id"] == "INC-TEST-001"
    assert item["outcome"] == "pending"
    assert item["accepted_by"] == "dev-oncall"
    assert item["accepted_at"] == "2026-09-05T12:05:00"
    assert isinstance(item["rca"], dict)
    assert item["rca"]["root_cause"] == "NPE in PaymentAdapter"
    assert item["rca"]["source"] == "gemini"


@pytest.mark.asyncio
async def test_developer_post_incident_and_accept_endpoints(monkeypatch):
    """Verify POST /developer/incidents and POST /developer/incidents/{id}/accept endpoints."""
    from tests.unit.nexus.test_db_helpers import MockPostgresClient

    mock_client = MockPostgresClient()

    async def _mock_get_db():
        return mock_client

    monkeypatch.setattr(
        "nexus.services.observability.dashboard.dashboard.get_database_client",
        _mock_get_db,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. POST a new incident (simulating demo_orchestrator)
        payload = {
            "incident_id": "INC-DEMO-999",
            "runbook_id": "runbook_pod_crashloop_v1",
            "target": "shop-demo",
            "level": 1,
            "outcome": "success",
            "description": "Restarted shop-demo which was crash-looping. Successfully resolved.",
            "confidence": 0.98,
        }
        res_post = await client.post("/developer/incidents", json=payload)
        assert res_post.status_code == 200
        data_post = res_post.json()
        assert data_post["status"] == "ok"
        assert data_post["incident_id"] == "INC-DEMO-999"

        # 2. GET /developer/incidents should contain the newly posted incident
        res_get = await client.get("/developer/incidents?n=10")
        assert res_get.status_code == 200
        items = res_get.json()
        match = next((i for i in items if i.get("incident_id") == "INC-DEMO-999"), None)
        assert match is not None
        assert match["target"] == "shop-demo"
        assert match["outcome"] == "success"
        assert match["level"] == 1
        assert "Restarted shop-demo" in match["description"]

        # 3. POST /developer/incidents/{incident_id}/accept claims ownership
        res_accept = await client.post(
            "/developer/incidents/INC-DEMO-999/accept",
            json={"user": "sre-alice"},
        )
        assert res_accept.status_code == 200
        data_accept = res_accept.json()
        assert data_accept["status"] == "ok"
        assert data_accept["accepted_by"] == "sre-alice"

        # 4. Verify updated accepted_by in GET /developer/incidents
        res_get_after = await client.get("/developer/incidents?n=10")
        items_after = res_get_after.json()
        match_after = next(
            (i for i in items_after if i.get("incident_id") == "INC-DEMO-999"), None
        )
        assert match_after is not None
        assert match_after["accepted_by"] == "sre-alice"
        assert match_after.get("accepted_at") is not None


@pytest.mark.asyncio
async def test_developer_post_incident_offline_fallback(monkeypatch):
    """Verify POST /developer/incidents works even when PostgreSQL is not configured."""

    async def _failing_db():
        raise RuntimeError("No PostgreSQL DSN configured")

    monkeypatch.setattr(
        "nexus.services.observability.dashboard.dashboard.get_database_client",
        _failing_db,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "incident_id": "INC-OFFLINE-123",
            "runbook_id": "runbook_missing_env_key_v1",
            "target": "checkout-offline",
            "level": 2,
            "outcome": "failed",
            "description": "Blocked deployment of checkout-offline — missing ENV.",
            "confidence": 0.85,
        }
        res_post = await client.post("/developer/incidents", json=payload)
        assert res_post.status_code == 200
        assert res_post.json()["incident_id"] == "INC-OFFLINE-123"

        res_get = await client.get("/developer/incidents?n=10&app=checkout-offline")
        assert res_get.status_code == 200
        items = res_get.json()
        assert len(items) >= 1
        assert items[0]["incident_id"] == "INC-OFFLINE-123"
        assert items[0]["outcome"] == "failed"
