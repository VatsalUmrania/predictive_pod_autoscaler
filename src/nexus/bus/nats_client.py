"""NEXUS NATS JetStream Client (Facade).

Backward-compatible entry point re-exporting NATSClient and constants.
"""

from nexus.bus.nats import (
    _PPA_STREAM_CONFIG,
    _STREAM_CONFIG,
    NEXUS_STREAM,
    NEXUS_SUBJECT,
    NEXUS_SUBJECTS,
    PPA_PREDICTIONS_SUBJECT,
    PPA_STREAM,
    NATSClient,
)

NatsClient = NATSClient

__all__ = [
    "NATSClient",
    "NatsClient",
    "NEXUS_STREAM",
    "NEXUS_SUBJECT",
    "NEXUS_SUBJECTS",
    "PPA_PREDICTIONS_SUBJECT",
    "PPA_STREAM",
    "_PPA_STREAM_CONFIG",
    "_STREAM_CONFIG",
]
