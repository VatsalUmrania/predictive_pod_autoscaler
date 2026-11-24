"""Tests for PostgreSQL persistence of diagnostic logs and remediation YAML."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.services.observability.dashboard.dashboard_incidents import (
    record_incident_resolution,
)


@pytest.mark.asyncio
async def test_record_incident_resolution_persists_yaml_and_logs():
    """Verify record_incident_resolution extracts/formats yaml and logs and calls write."""
    incident_data = {
        "incident_id": "INC-9999",
        "target": "default/shop-demo",
        "outcome": "success",
        "confidence": 0.95,
        "plan": {
            "actions": [
                {
                    "tool": "kubernetes_scale_deployment",
                    "arguments": {
                        "namespace": "default",
                        "deployment": "shop-demo",
                        "replicas": 3,
                    },
                }
            ]
        },
        "diagnostic_logs": "Pod logs: Connection timeout to database",
        "rca": {
            "failure_class": "db_pressure",
            "root_cause": "connection pool exhausted",
        },
    }

    mock_db = MagicMock()
    mock_conn = MagicMock()
    mock_conn.execute = AsyncMock()

    # Setup async context manager for db.acquire()
    mock_db.acquire.return_value.__aenter__.return_value = mock_conn
    mock_db.acquire.return_value.__aexit__.return_value = None

    await record_incident_resolution(incident_data, client=mock_db)

    # Verify conn.execute was called
    assert mock_conn.execute.call_count >= 2
    # Check the INSERT call args
    insert_call = mock_conn.execute.call_args_list[1]
    args = insert_call.args
    assert args[1] == "INC-9999"
    # args[12] is diagnostic_logs, args[13] is remediation_yaml
    assert args[12] == "Pod logs: Connection timeout to database"
    assert "replicas: 3" in args[13]
