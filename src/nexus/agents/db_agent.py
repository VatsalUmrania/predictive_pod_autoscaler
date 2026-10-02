"""
NEXUS Database Agent Facade
===========================
Backward-compatibility facade for DBAgent and related adapters.
"""

from nexus.agents.db import (
    DBAgent,
    DBConnectionStats,
    MongoDBAdapter,
    MySQLAdapter,
    PostgresAdapter,
    QuerySnapshot,
    SlowQuery,
    db_agent_from_env,
)

__all__ = [
    "DBAgent",
    "db_agent_from_env",
    "DBConnectionStats",
    "SlowQuery",
    "QuerySnapshot",
    "PostgresAdapter",
    "MySQLAdapter",
    "MongoDBAdapter",
]
