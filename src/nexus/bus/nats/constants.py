"""Stream configurations and subject constants for NATS JetStream."""
from __future__ import annotations

from nats.js.api import RetentionPolicy, StorageType, StreamConfig

NEXUS_STREAM = "NEXUS_INCIDENTS"
NEXUS_SUBJECT = "nexus.incidents"
NEXUS_SUBJECTS = "nexus.>"
PPA_PREDICTIONS_SUBJECT = "ppa.predictions.>"
PPA_STREAM = "PPA_PREDICTIONS"

# JetStream stream config: retain 24h of messages, max 50MB
_STREAM_CONFIG = StreamConfig(
    name=NEXUS_STREAM,
    subjects=[NEXUS_SUBJECTS],
    retention=RetentionPolicy.LIMITS,
    storage=StorageType.MEMORY,
    max_age=86_400,
    max_bytes=50 * 1024 * 1024,
    duplicate_window=60,
)

# PPA prediction/outcome stream
_PPA_STREAM_CONFIG = StreamConfig(
    name=PPA_STREAM,
    subjects=["ppa.predictions.>", "ppa.outcomes.>"],
    retention=RetentionPolicy.LIMITS,
    storage=StorageType.MEMORY,
    max_age=3600,
    max_bytes=10 * 1024 * 1024,
)
