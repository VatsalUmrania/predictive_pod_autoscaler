"""NEXUS PostgreSQL Layer (Facade).

Backward-compatible entry point re-exporting PostgresClient and factory.
"""

from nexus.db.client import PostgresClient, _global_db_client, get_database_client
from nexus.db.utils import _ensure_uuid

__all__ = [
    "PostgresClient",
    "_ensure_uuid",
    "_global_db_client",
    "get_database_client",
]
