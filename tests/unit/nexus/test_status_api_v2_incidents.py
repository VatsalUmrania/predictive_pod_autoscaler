import pytest
from httpx import ASGITransport, AsyncClient

from nexus.observability.status_api import app


@pytest.mark.asyncio
async def test_api_v2_incidents_crud_and_traces():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Ingest incident
        ingest_payload = {
            "fingerprint": "fp-api-test-1",
            "environment": "aws",
            "target_resource": "payment-processor-lambda",
            "severity": "critical",
            "trigger_source": "cloudwatch_alarm",
            "trigger_payload": {"metric": "Errors", "threshold": 5, "value": 12},
        }
        res = await client.post("/api/v2/incidents", json=ingest_payload)
        assert res.status_code == 200
        data = res.json()
        assert "incident_id" in data
        assert data["status"] == "detected"
        inc_id = data["incident_id"]

        # 2. List incidents
        res = await client.get("/api/v2/incidents")
        assert res.status_code == 200
        inc_list = res.json()
        assert any(i["incident_id"] == inc_id for i in inc_list)

        # 3. Get single incident
        res = await client.get(f"/api/v2/incidents/{inc_id}")
        assert res.status_code == 200
        inc_detail = res.json()
        assert inc_detail["fingerprint"] == "fp-api-test-1"
        assert inc_detail["environment"] == "aws"
        assert inc_detail["target_resource"] == "payment-processor-lambda"

        # 4. Get traces
        res = await client.get(f"/api/v2/incidents/{inc_id}/traces")
        assert res.status_code == 200
        trace = res.json()
        assert "incident" in trace
        assert "state_transitions" in trace
        assert len(trace["state_transitions"]) >= 1

        # 5. 404 for unknown incident
        res = await client.get("/api/v2/incidents/00000000-0000-0000-0000-000000000000")
        assert res.status_code == 404
