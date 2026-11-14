"""
MongoDB Adapter for DBAgent
"""

from __future__ import annotations

import logging

from nexus.agents.db.models import DBConnectionStats, QuerySnapshot, SlowQuery

logger = logging.getLogger(__name__)


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
        self.uri = uri
        self.database = database
        self._client = None

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

    async def connection_stats(self) -> DBConnectionStats | None:
        try:
            client = self._get_client()
            status = await client[self.database].command("serverStatus")
            conns = status.get("connections", {})
            current = int(conns.get("current", 0))
            avail = int(conns.get("available", 1))
            total = current + avail
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

    async def slow_queries(self, threshold_ms: float = 1000.0) -> list[SlowQuery]:
        try:
            client = self._get_client()
            ops = await client[self.database].command(
                "currentOp",
                {"active": True, "secs_running": {"$gte": int(threshold_ms / 1000)}},
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
