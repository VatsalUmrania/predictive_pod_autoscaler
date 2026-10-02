"""
PostgreSQL Adapter for DBAgent
"""

from __future__ import annotations

import logging

from nexus.agents.db.models import DBConnectionStats, QuerySnapshot, SlowQuery

logger = logging.getLogger(__name__)

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
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password
        self.connect_timeout = connect_timeout
        self._pool = None

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
                logger.info(
                    f"[DBAgent/Postgres] Connected to {self.host}:{self.port}/{self.database}"
                )
            except Exception as exc:
                logger.warning(f"[DBAgent/Postgres] Connection failed: {exc}")
                raise
        return self._pool

    async def connection_stats(self) -> DBConnectionStats | None:
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
                maxc = int(row["max_conn"])
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

    async def slow_queries(self, threshold_ms: float = 1000.0) -> list[SlowQuery]:
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT query, mean_exec_time, calls
                    FROM pg_stat_statements
                    WHERE mean_exec_time > $1
                    ORDER BY mean_exec_time DESC
                    LIMIT 10
                """,
                    threshold_ms,
                )
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
            logger.debug(
                f"[DBAgent/Postgres] query_pattern_snapshot unavailable: {exc}"
            )
        return snapshot

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
