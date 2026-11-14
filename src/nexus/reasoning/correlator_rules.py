"""
Event Correlator Rules and Filters
"""

from __future__ import annotations

# Events that carry information but should not trigger RCA on their own
SKIP_CLUSTERING = frozenset(
    {
        "db_query_spike",
        "metric_unavailable",
        "circuit_breaker_tripped",
        "deploy_event",
    }
)

# Single-event signals severe enough to emit a cluster immediately without quorum
IMMEDIATE_EMIT = frozenset(
    {
        "env_contract_violation",
        "secret_committed",
        "pod_crashloop",
        "pod_oomkilled",
        "dns_resolution_failure",
    }
)
