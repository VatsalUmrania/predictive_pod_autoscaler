"""
Unit tests for the NEXUS developer dashboard's agent-activity endpoints and
the operator authentication that guards the control plane.

Covers:
  - GET /developer/incidents/{id}/agent-runs  and  .../messages  response shapes
  - graceful degradation when PostgreSQL is unavailable
  - operator bearer-token authentication on the protected routes
  - the CORS allowlist
  - the agent-run producer: the reasoning nodes record their runs and messages
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from nexus.services.api.status.status_api import app
from nexus.services.gateway.auth.operator_auth import (
    ADMIN_TOKEN_ENV,
    DISABLE_AUTH_ENV,
    require_operator,
)

RUN_KEYS = {
    "run_id",
    "incident_id",
    "node_name",
    "agent_type",
    "model_name",
    "status",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "duration_ms",
    "error_message",
    "created_at",
    "finished_at",
}

MESSAGE_KEYS = {
    "message_id",
    "run_id",
    "incident_id",
    "sequence_num",
    "role",
    "content",
    "reasoning_content",
    "raw_payload",
    "created_at",
}


class _FakeAgentDb:
    """Minimal stand-in for the agent-run read path."""

    def __init__(self, runs: list[dict[str, Any]], messages: list[dict[str, Any]]):
        self._runs = runs
        self._messages = messages

    async def list_agent_runs(self, incident_id: str) -> list[dict[str, Any]]:
        return list(self._runs)

    async def list_agent_messages(self, incident_id: str) -> list[dict[str, Any]]:
        return list(self._messages)


def _sample_run(incident_id: str) -> dict[str, Any]:
    return {
        "run_id": str(uuid.uuid4()),
        "incident_id": incident_id,
        "node_name": "diagnose",
        "agent_type": "diagnose",
        "model_name": "gemini-3.1-flash-lite",
        "status": "completed",
        "prompt_tokens": 120,
        "completion_tokens": 45,
        "total_tokens": 165,
        "duration_ms": 812,
        "error_message": None,
        "created_at": "2026-11-25T10:00:00+00:00",
        "finished_at": "2026-11-25T10:00:01+00:00",
    }


def _sample_message(incident_id: str, run_id: str) -> dict[str, Any]:
    return {
        "message_id": str(uuid.uuid4()),
        "run_id": run_id,
        "incident_id": incident_id,
        "sequence_num": 1,
        "role": "assistant",
        "content": "Pod is CrashLoopBackOff.",
        "reasoning_content": "Checked the readiness probe.",
        "raw_payload": {"provider": "gemini"},
        "created_at": "2026-11-25T10:00:00+00:00",
    }


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _with_db_client(monkeypatch, db: Any) -> None:
    """Install a database client the dashboard's read path will pick up."""
    from nexus.services.api.status.status_context import context

    monkeypatch.setattr(context, "db_client", db, raising=False)


@pytest.mark.asyncio
async def test_agent_runs_endpoint_returns_the_documented_shape(monkeypatch):
    """GET /developer/incidents/{id}/agent-runs returns exactly NexusAgentRun."""
    incident_id = str(uuid.uuid4())
    run = _sample_run(incident_id)

    _with_db_client(monkeypatch, _FakeAgentDb([run], []))

    async with await _client() as client:
        res = await client.get(f"/developer/incidents/{incident_id}/agent-runs")

    assert res.status_code == 200
    body = res.json()
    assert len(body) == 1
    assert set(body[0].keys()) == RUN_KEYS
    assert body[0]["node_name"] == "diagnose"
    assert body[0]["status"] == "completed"
    assert body[0]["total_tokens"] == 165


@pytest.mark.asyncio
async def test_messages_endpoint_returns_the_documented_shape(monkeypatch):
    """GET /developer/incidents/{id}/messages returns exactly NexusAgentMessage."""
    incident_id = str(uuid.uuid4())
    run = _sample_run(incident_id)
    message = _sample_message(incident_id, run["run_id"])

    _with_db_client(monkeypatch, _FakeAgentDb([run], [message]))

    async with await _client() as client:
        res = await client.get(f"/developer/incidents/{incident_id}/messages")

    assert res.status_code == 200
    body = res.json()
    assert len(body) == 1
    assert set(body[0].keys()) == MESSAGE_KEYS
    assert body[0]["incident_id"] == incident_id
    assert body[0]["role"] == "assistant"


@pytest.mark.asyncio
async def test_agent_endpoints_degrade_when_postgres_is_unavailable(monkeypatch):
    """A missing database yields 200 [] rather than a 500."""
    incident_id = str(uuid.uuid4())

    async def _failing_db():
        raise RuntimeError("No PostgreSQL DSN configured.")

    monkeypatch.setattr(
        "nexus.services.observability.dashboard.dashboard.get_database_client",
        _failing_db,
    )

    async with await _client() as client:
        runs = await client.get(f"/developer/incidents/{incident_id}/agent-runs")
        messages = await client.get(f"/developer/incidents/{incident_id}/messages")

    assert runs.status_code == 200
    assert runs.json() == []
    assert messages.status_code == 200
    assert messages.json() == []


@pytest.mark.asyncio
async def test_agent_endpoints_tolerate_a_non_uuid_incident_id(monkeypatch):
    """Demo incidents use short string ids; they must not raise."""

    _with_db_client(monkeypatch, _FakeAgentDb([], []))

    async with await _client() as client:
        runs = await client.get("/developer/incidents/INC-DEMO-999/agent-runs")
        messages = await client.get("/developer/incidents/INC-DEMO-999/messages")

    assert runs.status_code == 200
    assert runs.json() == []
    assert messages.status_code == 200
    assert messages.json() == []


# ── Operator authentication ──────────────────────────────────────────────────


@pytest.fixture
def no_auth_override():
    """Drop the directory-wide auth override so the real check runs."""
    app.dependency_overrides.pop(require_operator, None)
    yield
    app.dependency_overrides[require_operator] = lambda: "test-operator"


@pytest.mark.asyncio
async def test_operator_routes_require_a_bearer_token(
    no_auth_override, monkeypatch
):
    """Protected routes answer 401 without a token, 200 with a valid one."""
    monkeypatch.setenv(ADMIN_TOKEN_ENV, "operator-secret")

    async with await _client() as client:
        missing = await client.get("/developer/incidents")
        wrong = await client.get(
            "/developer/incidents", headers={"Authorization": "Bearer nope"}
        )
        no_prefix = await client.get(
            "/developer/incidents", headers={"Authorization": "operator-secret"}
        )
        good = await client.get(
            "/developer/incidents",
            headers={"Authorization": "Bearer operator-secret"},
        )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert no_prefix.status_code == 401
    assert good.status_code == 200


@pytest.mark.asyncio
async def test_approve_and_reject_are_protected(no_auth_override, monkeypatch):
    """The remediation-executing endpoints must not be reachable anonymously."""
    monkeypatch.setenv(ADMIN_TOKEN_ENV, "operator-secret")

    async with await _client() as client:
        approve = await client.post("/approve/ACTION-1", json={})
        reject = await client.post("/reject/ACTION-1", json={})
        dashboard_approve = await client.post(
            "/developer/incidents/INC-1/approve", json={}
        )

    assert approve.status_code == 401
    assert reject.status_code == 401
    assert dashboard_approve.status_code == 401


@pytest.mark.asyncio
async def test_operator_routes_fail_closed_without_a_configured_token(
    no_auth_override, monkeypatch
):
    """No configured token means the routes are refused, not silently open."""
    monkeypatch.delenv(ADMIN_TOKEN_ENV, raising=False)
    monkeypatch.delenv(DISABLE_AUTH_ENV, raising=False)

    async with await _client() as client:
        res = await client.get("/developer/incidents")

    assert res.status_code == 503
    assert ADMIN_TOKEN_ENV in res.json()["detail"]


@pytest.mark.asyncio
async def test_public_routes_stay_open(no_auth_override, monkeypatch):
    """Health, status and metrics are read by probes and Prometheus."""
    monkeypatch.delenv(ADMIN_TOKEN_ENV, raising=False)
    monkeypatch.delenv(DISABLE_AUTH_ENV, raising=False)

    async with await _client() as client:
        health = await client.get("/health")
        status = await client.get("/status")
        metrics = await client.get("/metrics")

    assert health.status_code == 200
    assert status.status_code == 200
    assert metrics.status_code == 200


@pytest.mark.asyncio
async def test_cors_only_grants_configured_origins(no_auth_override, monkeypatch):
    """The wildcard CORS policy is gone."""
    monkeypatch.setenv("NEXUS_CORS_ORIGINS", "http://localhost:3000")

    # The middleware is built at import time, so rebuild the app's middleware
    # stack by re-reading the configured origins through the same helper.
    from nexus.services.api.status.status_api import _cors_origins

    assert _cors_origins() == ["http://localhost:3000"]

    async with await _client() as client:
        allowed = await client.get("/health", headers={"Origin": "http://localhost:3000"})
        denied = await client.get("/health", headers={"Origin": "http://evil.example"})

    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:3000"
    assert denied.headers.get("access-control-allow-origin") is None


# ── Producer ─────────────────────────────────────────────────────────────────


class _RecordingDb:
    """Captures the writes the workflow's reasoning nodes perform."""

    def __init__(self) -> None:
        self.incidents: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []

    async def create_incident(self, **kwargs: Any) -> str:
        self.incidents.append(kwargs)
        return str(kwargs.get("incident_id"))

    async def update_discovered_target(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def start_agent_run(
        self, incident_id: str, agent_type: str, model_name: str
    ) -> str:
        run_id = str(uuid.uuid4())
        self.runs.append(
            {
                "run_id": run_id,
                "incident_id": incident_id,
                "agent_type": agent_type,
                "model_name": model_name,
                "status": "running",
            }
        )
        return run_id

    async def finish_agent_run(self, run_id: str, **kwargs: Any) -> None:
        for run in self.runs:
            if run["run_id"] == run_id:
                run.update(kwargs)
                run["status"] = kwargs.get("status", run["status"])

    async def log_message(
        self,
        run_id: str,
        sequence_num: int,
        role: str,
        content: str,
        **kwargs: Any,
    ) -> str:
        message_id = str(uuid.uuid4())
        self.messages.append(
            {
                "message_id": message_id,
                "run_id": run_id,
                "sequence_num": sequence_num,
                "role": role,
                "content": content,
                **kwargs,
            }
        )
        return message_id


@pytest.mark.asyncio
async def test_workflow_records_agent_runs_and_messages(monkeypatch):
    """The reasoning nodes write their runs and messages for the incident."""
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import MemorySaver

    from nexus.engine.reasoning.rca.result import (
        DiscoveredTarget,
        RCAResult,
        VerificationResult,
    )
    from nexus.engine.workflow.graph.checkpointer import GraphCheckpointStore
    from nexus.engine.workflow.graph.workflow import IncidentWorkflow
    from nexus.services.api.status.status_context import context
    from nexus.services.observability.dashboard.dashboard_incidents import (
        _in_memory_incidents,
    )

    rca = RCAResult(
        target=DiscoveredTarget(
            platform="k8s",
            namespace_or_region="production",
            resource_kind="Deployment",
            resource_name="shop-demo",
        ),
        failure_type="readiness_probe_exit_zero",
        root_cause_summary="Readiness probe failed on shop-demo.",
        confidence=0.94,
        evidence=["kubectl describe pod shop-demo-abc123"],
        can_auto_remediate=True,
        recommended_strategy="Rollback Deployment/shop-demo.",
    )
    verif = VerificationResult(
        verification_passed=True,
        observed_state_summary="Deployment shop-demo readyReplicas=2/2.",
    )

    class _FakeStructured:
        def __init__(self, schema: Any):
            self._schema = schema

        async def ainvoke(self, messages: Any) -> Any:
            if self._schema is RCAResult:
                return rca
            if self._schema is VerificationResult:
                return verif
            raise ValueError(f"Unexpected schema {self._schema}")

    class _FakeLLM:
        async def ainvoke(self, messages: Any) -> AIMessage:
            return AIMessage(content="Observation complete", tool_calls=[])

        def bind_tools(self, tools: list[Any]) -> Any:
            return self

        def with_structured_output(self, schema: Any) -> Any:
            return _FakeStructured(schema)

    fake_llm = _FakeLLM()
    mock_registry = MagicMock()
    mock_registry.get_tools.side_effect = lambda mode, platform=None: []
    mock_registry.get_metadata.return_value = MagicMock(
        blast_radius="service_wide", reversible=True
    )

    db = _RecordingDb()
    workflow = IncidentWorkflow(checkpointer_store=GraphCheckpointStore(MemorySaver()))

    prev_wf = context.workflow
    prev_db = context.db_client
    prev_mem = list(_in_memory_incidents)
    _in_memory_incidents.clear()
    context.workflow = workflow
    context.db_client = db

    try:
        with (
            patch(
                "nexus.engine.nodes.analysis.diagnose.get_chat_model",
                return_value=fake_llm,
            ),
            patch(
                "nexus.engine.nodes.planning.plan.get_chat_model", return_value=fake_llm
            ),
            patch(
                "nexus.engine.nodes.execution.verify.get_chat_model",
                return_value=fake_llm,
            ),
            patch(
                "nexus.engine.nodes.analysis.diagnose.get_tool_registry",
                return_value=mock_registry,
            ),
            patch(
                "nexus.engine.nodes.planning.plan.get_tool_registry",
                return_value=mock_registry,
            ),
            patch(
                "nexus.engine.nodes.execution.execute.get_tool_registry",
                return_value=mock_registry,
            ),
            patch(
                "nexus.engine.nodes.execution.verify.get_tool_registry",
                return_value=mock_registry,
            ),
            patch(
                "nexus.engine.nodes.supervision.govern.evaluate_opa_policy",
                AsyncMock(
                    return_value={
                        "allowed": True,
                        "requires_approval": False,
                        "reason": "auto",
                    }
                ),
            ),
            patch(
                "nexus.engine.workflow.graph.workflow.send_approval_request_notification",
                AsyncMock(),
            ),
            patch(
                "nexus.services.reporting.incident_report.publish_post_incident_report",
                AsyncMock(),
            ),
        ):
            await workflow.run_incident(
                {
                    "incident_id": "INC-AGENT-01",
                    "platform": "k8s",
                    "namespace": "production",
                    "service_name": "shop-demo",
                    "raw_events": [
                        {
                            "involved_object_kind": "Pod",
                            "involved_object_name": "shop-demo-abc123",
                            "message": "Readiness probe failed",
                        }
                    ],
                },
                thread_id="INC-AGENT-01",
            )
    finally:
        context.workflow = prev_wf
        context.db_client = prev_db
        _in_memory_incidents.clear()
        _in_memory_incidents.extend(prev_mem)

    # The FK parent must exist before any agent run can be recorded.
    assert len(db.incidents) == 1
    assert db.incidents[0]["incident_id"] == "INC-AGENT-01"

    # diagnose is the node that always runs before the graph can branch.
    assert db.runs, "expected at least one agent run to be recorded"
    assert {run["agent_type"] for run in db.runs} >= {"diagnose"}
    assert all(run["status"] != "running" for run in db.runs)
    assert db.messages, "expected agent messages to be recorded"
    assert all(message["role"] in ("assistant", "tool") for message in db.messages)


# ── Row serialization ────────────────────────────────────────────────────────


def test_agent_run_row_maps_the_database_columns():
    """asyncpg rows are normalized into the dashboard's agent-run shape."""
    from datetime import datetime, timezone

    from nexus.core.database.repositories.agent_repo import _agent_run_row

    run_id = uuid.uuid4()
    incident_id = uuid.uuid4()
    created = datetime(2026, 11, 25, 10, 0, 0, tzinfo=timezone.utc)

    row = _agent_run_row(
        {
            "run_id": run_id,
            "incident_id": incident_id,
            "agent_type": "diagnose",
            "model_name": "gemini-3.1-flash-lite",
            "status": "completed",
            "prompt_tokens": 120,
            "completion_tokens": 45,
            "total_tokens": 165,
            "duration_ms": 812,
            "error_message": None,
            "created_at": created,
            "finished_at": created,
        }
    )

    assert set(row.keys()) == RUN_KEYS
    assert row["run_id"] == str(run_id)
    assert row["incident_id"] == str(incident_id)
    # agent_runs has no node_name column; agent_type carries the graph node.
    assert row["node_name"] == "diagnose"
    assert row["agent_type"] == "diagnose"
    assert row["created_at"] == created.isoformat()
    assert row["finished_at"] == created.isoformat()
    assert row["total_tokens"] == 165


def test_agent_message_row_joins_the_parent_incident_and_parses_jsonb():
    """agent_messages has no incident_id column and asyncpg returns jsonb as str."""
    from nexus.core.database.repositories.agent_repo import _agent_message_row

    message_id = uuid.uuid4()
    run_id = uuid.uuid4()
    incident_id = uuid.uuid4()

    row = _agent_message_row(
        {
            "message_id": message_id,
            "run_id": run_id,
            "parent_incident_id": incident_id,
            "sequence_num": 3,
            "role": "tool",
            "content": "Pod is CrashLoopBackOff",
            "reasoning_content": None,
            "raw_payload": '{"tool": "kubectl_get_pods"}',
            "created_at": "2026-11-25T10:00:00+00:00",
        }
    )

    assert set(row.keys()) == MESSAGE_KEYS
    assert row["message_id"] == str(message_id)
    assert row["run_id"] == str(run_id)
    assert row["incident_id"] == str(incident_id)
    assert row["sequence_num"] == 3
    assert row["raw_payload"] == {"tool": "kubectl_get_pods"}
