"""
NEXUS Database Module
=====================
Relational persistence layer for incidents, agent traces, tool calls, and governance audits.
"""

from nexus.db.postgres import (
    PostgresClient,
    SQLiteFallbackClient,
    get_database_client,
)

__all__ = [
    "PostgresClient",
    "SQLiteFallbackClient",
    "get_database_client",
]
