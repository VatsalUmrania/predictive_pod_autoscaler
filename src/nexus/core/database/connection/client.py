"""Unified PostgresClient facade composing repositories and connection pool."""

from __future__ import annotations

import os
from typing import Any

from nexus.core.database.connection.pool import PostgresPool
from nexus.core.database.repositories.agent_repo import AgentRepository
from nexus.core.database.repositories.incident_repo import IncidentRepository
from nexus.core.database.repositories.remediation_repo import RemediationRepository


class PostgresClient:
    """PostgreSQL Client using asyncpg with connection pooling and schema initialization."""

    def __init__(self, dsn: str | None = None):
        self._pool_mgr = PostgresPool(dsn=dsn)
        self.dsn = self._pool_mgr.dsn
        self.min_pool = self._pool_mgr.min_pool
        self.max_pool = self._pool_mgr.max_pool
        self.acquire = self._pool_mgr.acquire
        self.initialize = self._pool_mgr.initialize
        self.close = self._pool_mgr.close

        self._incidents = IncidentRepository(self._pool_mgr)
        self._agents = AgentRepository(self._pool_mgr)
        self._remediation = RemediationRepository(self._pool_mgr)

    @property
    def pool(self) -> Any:
        return self._pool_mgr.raw_pool

    # Incidents delegation
    async def create_incident(self, **kwargs) -> str:
        return await self._incidents.create_incident(**kwargs)

    async def transition_state(
        self,
        incident_id: str,
        to_state: str,
        reason: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        await self._incidents.transition_state(incident_id, to_state, reason, metadata)

    async def get_incident(self, incident_id: str) -> dict[str, Any] | None:
        return await self._incidents.get_incident(incident_id)

    async def get_active_incident_by_target(
        self, target_resource: str
    ) -> dict[str, Any] | None:
        return await self._incidents.get_active_incident_by_target(target_resource)

    async def list_incidents(
        self, limit: int = 50, state: str | None = None
    ) -> list[dict[str, Any]]:
        return await self._incidents.list_incidents(limit, state)

    async def update_root_cause(
        self, incident_id: str, summary: str, confidence: float
    ) -> None:
        await self._incidents.update_root_cause(incident_id, summary, confidence)

    async def update_discovered_target(
        self,
        incident_id: str,
        *,
        target_resource: str,
        namespace: str | None = None,
        resource_kind: str | None = None,
        service_name: str | None = None,
        failure_class: str | None = None,
        root_cause_summary: str | None = None,
        confidence: float | None = None,
    ) -> None:
        await self._incidents.update_discovered_target(
            incident_id,
            target_resource=target_resource,
            namespace=namespace,
            resource_kind=resource_kind,
            service_name=service_name,
            failure_class=failure_class,
            root_cause_summary=root_cause_summary,
            confidence=confidence,
        )

    # Agent runs delegation
    async def start_agent_run(
        self, incident_id: str, agent_type: str, model_name: str
    ) -> str:
        return await self._agents.start_agent_run(incident_id, agent_type, model_name)

    async def finish_agent_run(self, run_id: str, **kwargs) -> None:
        await self._agents.finish_agent_run(run_id, **kwargs)

    async def log_message(
        self, run_id: str, sequence_num: int, role: str, content: str, **kwargs
    ) -> str:
        return await self._agents.log_message(
            run_id, sequence_num, role, content, **kwargs
        )

    async def log_tool_call(
        self,
        run_id: str,
        tool_name: str,
        tool_category: str,
        input_parameters: dict,
        **kwargs,
    ) -> str:
        return await self._agents.log_tool_call(
            run_id, tool_name, tool_category, input_parameters, **kwargs
        )

    async def update_tool_call(self, call_id: str, status: str, **kwargs) -> None:
        await self._agents.update_tool_call(call_id, status, **kwargs)

    async def list_agent_runs(self, incident_id: str) -> list[dict[str, Any]]:
        return await self._agents.list_agent_runs(incident_id)

    async def list_agent_messages(self, incident_id: str) -> list[dict[str, Any]]:
        return await self._agents.list_agent_messages(incident_id)

    # Remediation delegation
    async def create_remediation_action(self, **kwargs) -> str:
        return await self._remediation.create_remediation_action(**kwargs)

    async def update_remediation_action(
        self, action_id: str, outcome: str, **kwargs
    ) -> None:
        await self._remediation.update_remediation_action(action_id, outcome, **kwargs)

    async def get_incident_trace(self, incident_id: str) -> dict[str, Any]:
        return await self._remediation.get_incident_trace(incident_id)


_global_db_client: PostgresClient | None = None


async def get_database_client(dsn: str | None = None) -> PostgresClient:
    """Factory returning the global PostgresClient singleton."""
    global _global_db_client

    if _global_db_client is not None:
        return _global_db_client

    configured_dsn = dsn or os.getenv(
        "NEXUS_POSTGRES_DSN", os.getenv("DATABASE_URL", "")
    )
    if not configured_dsn:
        raise RuntimeError(
            "No PostgreSQL DSN configured. Set NEXUS_POSTGRES_DSN or DATABASE_URL."
        )

    client = PostgresClient(dsn=configured_dsn)
    await client.initialize()
    _global_db_client = client
    return client


async def get_optional_database_client() -> PostgresClient | None:
    """Return the live Postgres client, or None when Postgres is unavailable.

    Postgres is an optional dependency at runtime: the server boots and serves
    every route when the database is missing or unreachable. Callers that can
    degrade should use this instead of ``get_database_client()``, which raises
    and re-runs its connection retry ladder on every attempt.
    """
    try:
        from nexus.services.api.status.status_context import context

        client: PostgresClient | None = getattr(context, "db_client", None)
        if client is not None:
            return client
    except Exception:
        pass

    return _global_db_client
