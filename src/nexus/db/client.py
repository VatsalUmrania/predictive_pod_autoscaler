"""Unified PostgresClient facade composing repositories and connection pool."""

from __future__ import annotations

import os
from typing import Any

from nexus.db.pool import PostgresPool
from nexus.db.repositories.agent_repo import AgentRepository
from nexus.db.repositories.incident_repo import IncidentRepository
from nexus.db.repositories.remediation_repo import RemediationRepository


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
    import nexus.db.postgres as pg_mod

    # Keep pg_mod._global_db_client in sync if monkeypatched
    if (
        hasattr(pg_mod, "_global_db_client")
        and pg_mod._global_db_client is not _global_db_client
    ):
        _global_db_client = pg_mod._global_db_client

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
    if hasattr(pg_mod, "_global_db_client"):
        pg_mod._global_db_client = client
    return client
