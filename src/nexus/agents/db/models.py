"""
Database Agent Data Models
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DBConnectionStats:
    db_engine: str
    db_host: str
    active_connections: int
    max_connections: int
    utilization_pct: float
    blocking_query: str | None = None


@dataclass
class SlowQuery:
    query_digest: str
    avg_latency_ms: float
    calls: int
    database: str | None = None


@dataclass
class QuerySnapshot:
    """Timestamped per-table/collection query volume — input for DBTrafficCorrelator."""

    db_engine: str
    table_counts: dict[str, int] = field(default_factory=dict)
