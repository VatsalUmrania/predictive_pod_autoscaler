"""
DBAgent Factory from Environment Variables
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from nexus.agents.db.mongo import MongoDBAdapter
from nexus.agents.db.mysql import MySQLAdapter
from nexus.agents.db.postgres import PostgresAdapter
from nexus.bus.nats_client import NATSClient

if TYPE_CHECKING:
    from nexus.agents.db.agent import DBAgent

logger = logging.getLogger(__name__)


def db_agent_from_env(nats_client: NATSClient) -> DBAgent:
    """Build a DBAgent from environment variable configuration."""
    from nexus.agents.db.agent import DBAgent

    adapters = []

    if os.getenv("NEXUS_POSTGRES_HOST"):
        adapters.append(
            PostgresAdapter(
                host=os.environ["NEXUS_POSTGRES_HOST"],
                port=int(os.getenv("NEXUS_POSTGRES_PORT", "5432")),
                database=os.getenv("NEXUS_POSTGRES_DB", "postgres"),
                user=os.getenv("NEXUS_POSTGRES_USER", "postgres"),
                password=os.getenv("NEXUS_POSTGRES_PASSWORD", ""),
            )
        )

    if os.getenv("NEXUS_MYSQL_HOST"):
        adapters.append(
            MySQLAdapter(
                host=os.environ["NEXUS_MYSQL_HOST"],
                port=int(os.getenv("NEXUS_MYSQL_PORT", "3306")),
                database=os.getenv("NEXUS_MYSQL_DB", "mysql"),
                user=os.getenv("NEXUS_MYSQL_USER", "root"),
                password=os.getenv("NEXUS_MYSQL_PASSWORD", ""),
            )
        )

    if os.getenv("NEXUS_MONGODB_URI"):
        adapters.append(
            MongoDBAdapter(
                uri=os.environ["NEXUS_MONGODB_URI"],
                database=os.getenv("NEXUS_MONGODB_DB", "admin"),
            )
        )

    if not adapters:
        logger.warning(
            "[DBAgent] No DB adapters configured — set NEXUS_POSTGRES_HOST, NEXUS_MYSQL_HOST, or NEXUS_MONGODB_URI"
        )

    return DBAgent(nats_client=nats_client, adapters=adapters)
