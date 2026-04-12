"""
NEXUS Database Agent
=====================
Monitors PostgreSQL, MySQL, and MongoDB for connection pool exhaustion,
slow queries, and lock contention.

Novel contribution (Research gap #2):
    The DBTrafficCorrelator sub-module (Phase 5) will use the query pattern
    data collected here to predict HTTP route traffic 5–10 minutes ahead.
    Phase 2 collects the raw data and emits anomaly events only.

Architecture — multi-adapter pattern:
    DBAgent
    ├── PostgresAdapter  — pg_stat_activity, pg_stat_statements
    ├── MySQLAdapter     — performance_schema.events_statements_*
    └── MongoDBAdapter   — currentOp, serverStatus

Each adapter implements a common interface:
    async def connection_stats() → DBConnectionStats
    async def slow_queries(threshold_ms) → List[SlowQuery]
    async def query_pattern_snapshot() → QuerySnapshot  ← for DBTrafficCorrelator

Published IncidentEvents:
    DB_CONNECTION_EXHAUSTION  — connection utilization > threshold
    SLOW_QUERY_DETECTED       — queries exceeding latency threshold
    DB_LOCK_CONTENTION        — blocking queries detected
    DB_QUERY_SPIKE            — sudden surge in query volume (DBTrafficCorrelator input)

Configuration (per DB, passed as constructor args or env vars):
    NEXUS_DB_CONNECTION_THRESHOLD  (default: 80.0%)
    NEXUS_SLOW_QUERY_THRESHOLD_MS  (default: 1000ms)
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from nexus.agents.base_agent import BaseAgent
from nexus.bus.incident_event import (
    AgentType,
    DBConnectionExhaustionContext,
    IncidentEvent,
    Severity,
    SignalType,
)
from nexus.bus.nats_client import NATSClient

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Shared data types
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class DBConnectionStats:
    db_engine:          str
    db_host:            str
    active_connections: int
    max_connections:    int
    utilization_pct:    float
    blocking_query:     Optional[str] = None


@dataclass
class SlowQuery:
    query_digest:   str
    avg_latency_ms: float
    calls:          int
    database:       Optional[str] = None


@dataclass
class QuerySnapshot:
    """Timestamped per-table/collection query volume — input for DBTrafficCorrelator."""
    db_engine:    str
    table_counts: Dict[str, int] = field(default_factory=dict)   # table → read+write count


# ──────────────────────────────────────────────────────────────────────────────
# PostgreSQL Adapter
# ──────────────────────────────────────────────────────────────────────────────

class PostgresAdapter:
    """
    Monitors PostgreSQL via pg_stat_activity and pg_stat_statements.
    Uses asyncpg for non-blocking queries.
    """

    def __init__(
        self,
        host: str,
        port: int = 5432,
        database: str = "postgres",
        user: str = "postgres",
        password: str = "",
        connect_timeout: float = 5.0,
    ):
        self.host            = host
        self.port            = port
        self.database        = database
        self.user            = user
        self.password        = password
        self.connect_timeout = connect_timeout
        self._pool           = None

    async def _get_pool(self):
        if self._pool is None:
            try:
                import asyncpg
                self._pool = await asyncpg.create_pool(
                    host=self.host,
                    port=self.port,
                    database=self.database,
                    user=self.user,
                    password=self.password,
                    min_size=1,
                    max_size=3,
                    command_timeout=self.connect_timeout,
                )
                logger.info(f"[DBAgent/Postgres] Connected to {self.host}:{self.port}/{self.database}")
            except Exception as exc:
                logger.warning(f"[DBAgent/Postgres] Connection failed: {exc}")
                raise
        return self._pool

    async def connection_stats(self) -> Optional[DBConnectionStats]:
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow("""
                    SELECT
                        count(*) AS active,
                        current_setting('max_connections')::int AS max_conn,
                        (
                            SELECT query FROM pg_stat_activity
                            WHERE wait_event_type = 'Lock' AND state = 'active'
                            LIMIT 1
                        ) AS blocking_query
                    FROM pg_stat_activity
                    WHERE state = 'active'
                """)
                if not row:
                    return None
                active = int(row["active"])
                maxc   = int(row["max_conn"])
                return DBConnectionStats(
                    db_engine="postgres",
                    db_host=self.host,
                    active_connections=active,
                    max_connections=maxc,
                    utilization_pct=(active / maxc * 100) if maxc > 0 else 0.0,
                    blocking_query=row.get("blocking_query"),
                )
        except Exception as exc:
            logger.warning(f"[DBAgent/Postgres] connection_stats failed: {exc}")
            return None

    async def slow_queries(self, threshold_ms: float = 1000.0) -> List[SlowQuery]:
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                # pg_stat_statements may not be installed — catch gracefully
                rows = await conn.fetch("""
                    SELECT query, mean_exec_time, calls
                    FROM pg_stat_statements
                    WHERE mean_exec_time > $1
                    ORDER BY mean_exec_time DESC
                    LIMIT 10
                """, threshold_ms)
                return [
                    SlowQuery(
                        query_digest=r["query"][:200],
                        avg_latency_ms=float(r["mean_exec_time"]),
                        calls=int(r["calls"]),
                        database=self.database,
                    )
                    for r in rows
                ]
        except Exception as exc:
            logger.debug(f"[DBAgent/Postgres] slow_queries unavailable: {exc}")
            return []

    async def query_pattern_snapshot(self) -> QuerySnapshot:
        """Collect per-table query counts for the DBTrafficCorrelator."""
        snapshot = QuerySnapshot(db_engine="postgres")
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT relname AS table_name,
                           seq_scan + idx_scan AS reads,
                           n_tup_ins + n_tup_upd + n_tup_del AS writes
                    FROM pg_stat_user_tables
                    ORDER BY (seq_scan + idx_scan) DESC
                    LIMIT 30
                """)
                for r in rows:
                    snapshot.table_counts[r["table_name"]] = r["reads"] + r["writes"]
        except Exception as exc:
            logger.debug(f"[DBAgent/Postgres] query_pattern_snapshot unavailable: {exc}")
        return snapshot

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()


# ──────────────────────────────────────────────────────────────────────────────
# MySQL Adapter
# ──────────────────────────────────────────────────────────────────────────────

class MySQLAdapter:
    """
    Monitors MySQL/MariaDB via performance_schema.
    Uses aiomysql for non-blocking queries.
    """

    def __init__(
        self,
        host: str,
        port: int = 3306,
        database: str = "mysql",
        user: str = "root",
        password: str = "",
    ):
        self.host     = host
        self.port     = port
        self.database = database
        self.user     = user
        self.password = password
        self._pool    = None

    async def _get_pool(self):
        if self._pool is None:
            try:
                import aiomysql
                self._pool = await aiomysql.create_pool(
                    host=self.host,
                    port=self.port,
                    db=self.database,
                    user=self.user,
                    password=self.password,
                    minsize=1,
                    maxsize=3,
                )
                logger.info(f"[DBAgent/MySQL] Connected to {self.host}:{self.port}/{self.database}")
            except Exception as exc:
                logger.warning(f"[DBAgent/MySQL] Connection failed: {exc}")
                raise
        return self._pool

    async def connection_stats(self) -> Optional[DBConnectionStats]:
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SHOW STATUS LIKE 'Threads_connected'")
                    threads_row = await cur.fetchone()
                    await cur.execute("SHOW VARIABLES LIKE 'max_connections'")
                    max_row = await cur.fetchone()

                    active = int(threads_row[1]) if threads_row else 0
                    maxc   = int(max_row[1]) if max_row else 151
                    return DBConnectionStats(
                        db_engine="mysql",
                        db_host=self.host,
                        active_connections=active,
                        max_connections=maxc,
                        utilization_pct=(active / maxc * 100) if maxc > 0 else 0.0,
                    )
        except Exception as exc:
            logger.warning(f"[DBAgent/MySQL] connection_stats failed: {exc}")
            return None

    async def slow_queries(self, threshold_ms: float = 1000.0) -> List[SlowQuery]:
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("""
                        SELECT DIGEST_TEXT, AVG_TIMER_WAIT / 1e9, COUNT_STAR
                        FROM performance_schema.events_statements_summary_by_digest
                        WHERE AVG_TIMER_WAIT / 1e6 > %s
                        ORDER BY AVG_TIMER_WAIT DESC
                        LIMIT 10
                    """, (threshold_ms,))
                    rows = await cur.fetchall()
                    return [
                        SlowQuery(
                            query_digest=str(r[0])[:200],
                            avg_latency_ms=float(r[1]),
                            calls=int(r[2]),
                        )
                        for r in rows
                    ]
        except Exception as exc:
            logger.debug(f"[DBAgent/MySQL] slow_queries unavailable: {exc}")
            return []

    async def query_pattern_snapshot(self) -> QuerySnapshot:
        return QuerySnapshot(db_engine="mysql")

    async def close(self) -> None:
        if self._pool:
            self._pool.close()
            await self._pool.wait_closed()


# ──────────────────────────────────────────────────────────────────────────────
# MongoDB Adapter
# ──────────────────────────────────────────────────────────────────────────────

class MongoDBAdapter:
    """
    Monitors MongoDB via serverStatus and currentOp.
    Uses motor (async pymongo wrapper).
    """

    def __init__(
        self,
        uri: str = "mongodb://localhost:27017",
        database: str = "admin",
    ):
        self.uri      = uri
        self.database = database
        self._client  = None

    def _get_client(self):
        if self._client is None:
            try:
                import motor.motor_asyncio as motor
                self._client = motor.AsyncIOMotorClient(
                    self.uri,
                    serverSelectionTimeoutMS=5000,
                )
                logger.info(f"[DBAgent/MongoDB] Client created for {self.uri}")
            except Exception as exc:
                logger.warning(f"[DBAgent/MongoDB] Client creation failed: {exc}")
                raise
        return self._client

    async def connection_stats(self) -> Optional[DBConnectionStats]:
        try:
            client = self._get_client()
            status = await client[self.database].command("serverStatus")
            conns  = status.get("connections", {})
            current = int(conns.get("current", 0))
            avail   = int(conns.get("available", 1))
            total   = current + avail
            return DBConnectionStats(
                db_engine="mongodb",
                db_host=self.uri,
                active_connections=current,
                max_connections=total,
                utilization_pct=(current / total * 100) if total > 0 else 0.0,
            )
        except Exception as exc:
            logger.warning(f"[DBAgent/MongoDB] connection_stats failed: {exc}")
            return None

    async def slow_queries(self, threshold_ms: float = 1000.0) -> List[SlowQuery]:
        try:
            client = self._get_client()
            ops    = await client[self.database].command(
                "currentOp", {"active": True, "secs_running": {"$gte": int(threshold_ms / 1000)}}
            )
            return [
                SlowQuery(
                    query_digest=str(op.get("query", op.get("command", {})))[:200],
                    avg_latency_ms=float(op.get("secs_running", 0)) * 1000,
                    calls=1,
                )
                for op in ops.get("inprog", [])
            ]
        except Exception as exc:
            logger.debug(f"[DBAgent/MongoDB] slow_queries unavailable: {exc}")
            return []

    async def query_pattern_snapshot(self) -> QuerySnapshot:
        return QuerySnapshot(db_engine="mongodb")

    async def close(self) -> None:
        if self._client:
            self._client.close()


# ──────────────────────────────────────────────────────────────────────────────
# DB Agent
# ──────────────────────────────────────────────────────────────────────────────

_AdapterType = PostgresAdapter | MySQLAdapter | MongoDBAdapter


class DBAgent(BaseAgent):
    """
    Multi-database monitoring agent.

    Accepts a list of database adapters (Postgres, MySQL, MongoDB).
    Polls each adapter for connection stats, slow queries, and query patterns.

    Args:
        adapters:                    List of initialized DB adapters.
        connection_threshold_pct:    Connection utilization % to trigger alert (default 80%).
        slow_query_threshold_ms:     Query latency to flag as slow (default 1000ms).
        poll_interval_seconds:       How often to poll DBs (default 30s).
        namespace / deployment_name: K8s context for event targeting.
    """

    def __init__(
        self,
        nats_client: NATSClient,
        adapters: List[_AdapterType],
        connection_threshold_pct: float = 80.0,
        slow_query_threshold_ms: float = 1000.0,
        poll_interval_seconds: float = 30.0,
        namespace: Optional[str] = None,
        deployment_name: Optional[str] = None,
    ):
        super().__init__(
            nats_client           = nats_client,
            agent_type            = AgentType.DB,
            poll_interval_seconds = poll_interval_seconds,
        )
        self.adapters              = adapters
        self.conn_threshold        = float(os.getenv("NEXUS_DB_CONNECTION_THRESHOLD", str(connection_threshold_pct)))
        self.slow_threshold_ms     = float(os.getenv("NEXUS_SLOW_QUERY_THRESHOLD_MS", str(slow_query_threshold_ms)))
        self.namespace             = namespace
        self.deployment_name       = deployment_name

    # ── Per-adapter checks ────────────────────────────────────────────────────

    async def _check_adapter(self, adapter: _AdapterType) -> List[IncidentEvent]:
        events: List[IncidentEvent] = []

        # Connection stats
        stats = await adapter.connection_stats()
        if stats and stats.utilization_pct >= self.conn_threshold:
            events.append(IncidentEvent(
                agent=AgentType.DB,
                signal_type=SignalType.DB_CONNECTION_EXHAUSTION,
                severity=Severity.CRITICAL if stats.utilization_pct >= 95 else Severity.WARNING,
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
                suggested_runbook="runbook_db_connection_exhaustion_v1",
                suggested_healing_level=2,
                confidence=0.92,
            ))

            if stats.blocking_query:
                events.append(IncidentEvent(
                    agent=AgentType.DB,
                    signal_type=SignalType.DB_LOCK_CONTENTION,
                    severity=Severity.WARNING,
                    namespace=self.namespace,
                    context={
                        "db_engine":       stats.db_engine,
                        "db_host":         stats.db_host,
                        "blocking_query":  stats.blocking_query[:300],
                    },
                ))

        # Slow queries
        slow = await adapter.slow_queries(self.slow_threshold_ms)
        for sq in slow[:5]:   # Cap at 5 per adapter per cycle
            events.append(IncidentEvent(
                agent=AgentType.DB,
                signal_type=SignalType.SLOW_QUERY_DETECTED,
                severity=Severity.WARNING,
                namespace=self.namespace,
                context={
                    "db_engine":       adapter.__class__.__name__.replace("Adapter", "").lower(),
                    "query_digest":    sq.query_digest,
                    "avg_latency_ms":  sq.avg_latency_ms,
                    "calls":           sq.calls,
                    "database":        sq.database,
                },
            ))

        # Query pattern snapshot (for DBTrafficCorrelator in Phase 5)
        snapshot = await adapter.query_pattern_snapshot()
        if snapshot.table_counts:
            events.append(IncidentEvent(
                agent=AgentType.DB,
                signal_type=SignalType.DB_QUERY_SPIKE,
                severity=Severity.INFO,
                namespace=self.namespace,
                context={
                    "db_engine":    snapshot.db_engine,
                    "table_counts": snapshot.table_counts,
                    "type":         "query_pattern_snapshot",
                },
            ))

        return events

    # ── BaseAgent interface ───────────────────────────────────────────────────

    async def sense(self) -> List[IncidentEvent]:
        results = await asyncio.gather(
            *[self._check_adapter(adapter) for adapter in self.adapters],
            return_exceptions=True,
        )
        events: List[IncidentEvent] = []
        for r in results:
            if isinstance(r, Exception):
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


# ──────────────────────────────────────────────────────────────────────────────
# Factory helper
# ──────────────────────────────────────────────────────────────────────────────

def db_agent_from_env(nats_client: NATSClient) -> DBAgent:
    """
    Build a DBAgent from environment variable configuration.

    Supported env vars (set any combination):
        NEXUS_POSTGRES_HOST / PORT / DB / USER / PASSWORD
        NEXUS_MYSQL_HOST / PORT / DB / USER / PASSWORD
        NEXUS_MONGODB_URI / DB
    """
    adapters: List[_AdapterType] = []

    if os.getenv("NEXUS_POSTGRES_HOST"):
        adapters.append(PostgresAdapter(
            host=os.environ["NEXUS_POSTGRES_HOST"],
            port=int(os.getenv("NEXUS_POSTGRES_PORT", "5432")),
            database=os.getenv("NEXUS_POSTGRES_DB", "postgres"),
            user=os.getenv("NEXUS_POSTGRES_USER", "postgres"),
            password=os.getenv("NEXUS_POSTGRES_PASSWORD", ""),
        ))

    if os.getenv("NEXUS_MYSQL_HOST"):
        adapters.append(MySQLAdapter(
            host=os.environ["NEXUS_MYSQL_HOST"],
            port=int(os.getenv("NEXUS_MYSQL_PORT", "3306")),
            database=os.getenv("NEXUS_MYSQL_DB", "mysql"),
            user=os.getenv("NEXUS_MYSQL_USER", "root"),
            password=os.getenv("NEXUS_MYSQL_PASSWORD", ""),
        ))

    if os.getenv("NEXUS_MONGODB_URI"):
        adapters.append(MongoDBAdapter(
            uri=os.environ["NEXUS_MONGODB_URI"],
            database=os.getenv("NEXUS_MONGODB_DB", "admin"),
        ))

    if not adapters:
        logger.warning("[DBAgent] No DB adapters configured — set NEXUS_POSTGRES_HOST, NEXUS_MYSQL_HOST, or NEXUS_MONGODB_URI")

    return DBAgent(nats_client=nats_client, adapters=adapters)
