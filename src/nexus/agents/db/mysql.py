"""
MySQL Adapter for DBAgent
"""

from __future__ import annotations

import logging

from nexus.agents.db.models import DBConnectionStats, QuerySnapshot, SlowQuery

logger = logging.getLogger(__name__)

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
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password
        self._pool = None

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
                logger.info(
                    f"[DBAgent/MySQL] Connected to {self.host}:{self.port}/{self.database}"
                )
            except Exception as exc:
                logger.warning(f"[DBAgent/MySQL] Connection failed: {exc}")
                raise
        return self._pool

    async def connection_stats(self) -> DBConnectionStats | None:
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SHOW STATUS LIKE 'Threads_connected'")
                    threads_row = await cur.fetchone()
                    await cur.execute("SHOW VARIABLES LIKE 'max_connections'")
                    max_row = await cur.fetchone()

                    active = int(threads_row[1]) if threads_row else 0
                    maxc = int(max_row[1]) if max_row else 151
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

    async def slow_queries(self, threshold_ms: float = 1000.0) -> list[SlowQuery]:
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT DIGEST_TEXT, AVG_TIMER_WAIT / 1e9, COUNT_STAR
                        FROM performance_schema.events_statements_summary_by_digest
                        WHERE AVG_TIMER_WAIT / 1e6 > %s
                        ORDER BY AVG_TIMER_WAIT DESC
                        LIMIT 10
                    """,
                        (threshold_ms,),
                    )
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
