"""
Integration smoke test for NexusServer.

Exercises the full create() → start() → stop() lifecycle against a real
NATS server (started by the `nats_server` fixture). Tests are skipped when
SKIP_NATS_TESTS=1 — no NATS available.

The intent is wiring verification, not behaviour coverage. Per-component
logic is exercised in tests/unit/nexus/.
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("SKIP_NATS_TESTS") == "1",
    reason="SKIP_NATS_TESTS=1; no NATS server available",
)


@pytest.mark.asyncio
async def test_nexus_server_lifecycle_with_nats(nats_server):
    """Full create→start→stop cycle against a real NATS server."""
    from nexus.server import NexusServer

    nats_url = str(nats_server.uri)

    server = await NexusServer.create(
        nats_url=nats_url,
        runbook_dir=None,
        prometheus_url="http://prometheus:9090",
        gemini_api_key=None,
    )

    # server should have all components wired
    assert server.nats_client is not None
    assert server.orchestrator is not None
    assert server.prescaler is not None
    assert server.outcome_tracker is not None
    assert server.notifier is not None
    assert server.agent_manager is not None
    assert server.status_api is not None

    # start() should not raise
    await server.start()

    # Confirm agent tasks are running
    assert len(server._tasks) >= 1  # orchestrator + status-api + agents

    # stop() should not raise
    await server.stop()

    assert server._tasks == []
