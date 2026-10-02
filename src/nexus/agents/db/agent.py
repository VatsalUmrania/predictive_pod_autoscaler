"""
NEXUS Database Agent Implementation
"""

from __future__ import annotations

import asyncio
import logging
import os

from nexus.agents.base_agent import BaseAgent
from nexus.agents.db.factory import db_agent_from_env
from nexus.agents.db.mongo import MongoDBAdapter
from nexus.agents.db.mysql import MySQLAdapter
from nexus.agents.db.postgres import PostgresAdapter
from nexus.bus.incident_event import (
    AgentType,
    DBConnectionExhaustionContext,
    IncidentEvent,
    Severity,
    SignalType,
)
from nexus.bus.nats_client import NATSClient

logger = logging.getLogger(__name__)

_AdapterType = PostgresAdapter | MySQLAdapter | MongoDBAdapter

class DBAgent(BaseAgent):
    """
    Multi-database monitoring agent.

    Accepts a list of database adapters (Postgres, MySQL, MongoDB).
    Polls each adapter for connection stats, slow queries, and query patterns.
    """

    def __init__(
        self,
        nats_client: NATSClient,
        adapters: list[_AdapterType],
        connection_threshold_pct: float = 80.0,
        slow_query_threshold_ms: float = 1000.0,
        poll_interval_seconds: float = 30.0,
        namespace: str | None = None,
        deployment_name: str | None = None,
    ):
        super().__init__(
            nats_client=nats_client,
            agent_type=AgentType.DB,
            poll_interval_seconds=poll_interval_seconds,
        )
        self.adapters = adapters
        self.conn_threshold = float(
            os.getenv("NEXUS_DB_CONNECTION_THRESHOLD", str(connection_threshold_pct))
        )
        self.slow_threshold_ms = float(
            os.getenv("NEXUS_SLOW_QUERY_THRESHOLD_MS", str(slow_query_threshold_ms))
        )
        self.namespace = namespace
        self.deployment_name = deployment_name

    async def _check_adapter(self, adapter: _AdapterType) -> list[IncidentEvent]:
        events: list[IncidentEvent] = []

        stats = await adapter.connection_stats()
        if stats and stats.utilization_pct >= self.conn_threshold:
            events.append(
                IncidentEvent(
                    agent=AgentType.DB,
                    signal_type=SignalType.DB_CONNECTION_EXHAUSTION,
                    severity=(
                        Severity.CRITICAL
                        if stats.utilization_pct >= 95
                        else Severity.WARNING
                    ),
                    namespace=self.namespace,
                    resource_name=self.deployment_name,
                    context=DBConnectionExhaustionContext(
                        db_engine=stats.db_engine,
                        db_host=stats.db_host,
                        active_connections=stats.active_connections,
                        max_connections=stats.max_connections,
                        utilization_pct=stats.utilization_pct,
                        blocking_query=stats.blocking_query,
                    ).model_dump(),
                    suggested_healing_level=2,
                    confidence=0.92,
                )
            )

            if stats.blocking_query:
                events.append(
                    IncidentEvent(
                        agent=AgentType.DB,
                        signal_type=SignalType.DB_LOCK_CONTENTION,
                        severity=Severity.WARNING,
                        namespace=self.namespace,
                        context={
                            "db_engine": stats.db_engine,
                            "db_host": stats.db_host,
                            "blocking_query": stats.blocking_query[:300],
                        },
                    )
                )

        slow = await adapter.slow_queries(self.slow_threshold_ms)
        for sq in slow[:5]:
            events.append(
                IncidentEvent(
                    agent=AgentType.DB,
                    signal_type=SignalType.SLOW_QUERY_DETECTED,
                    severity=Severity.WARNING,
                    namespace=self.namespace,
                    context={
                        "db_engine": adapter.__class__.__name__.replace(
                            "Adapter", ""
                        ).lower(),
                        "query_digest": sq.query_digest,
                        "avg_latency_ms": sq.avg_latency_ms,
                        "calls": sq.calls,
                        "database": sq.database,
                    },
                )
            )

        snapshot = await adapter.query_pattern_snapshot()
        if snapshot.table_counts:
            events.append(
                IncidentEvent(
                    agent=AgentType.DB,
                    signal_type=SignalType.DB_QUERY_SPIKE,
                    severity=Severity.INFO,
                    namespace=self.namespace,
                    context={
                        "db_engine": snapshot.db_engine,
                        "table_counts": snapshot.table_counts,
                        "type": "query_pattern_snapshot",
                    },
                )
            )

        return events

    async def on_start(self) -> None:
        pass

    async def sense(self) -> list[IncidentEvent]:
        results = await asyncio.gather(
            *[self._check_adapter(adapter) for adapter in self.adapters],
            return_exceptions=True,
        )
        events: list[IncidentEvent] = []
        for r in results:
            if isinstance(r, BaseException):
                logger.warning(f"[DBAgent] Adapter check failed: {r}")
            else:
                events.extend(r)
        return events

    async def on_stop(self) -> None:
        for adapter in self.adapters:
            try:
                await adapter.close()
            except Exception:
                pass


__all__ = ["DBAgent", "db_agent_from_env"]

