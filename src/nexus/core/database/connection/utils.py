"""Database helper utilities."""

from __future__ import annotations

import uuid


def _ensure_uuid(val: str | None) -> uuid.UUID:
    """Ensure a value is converted to a valid UUID. If not a valid hex UUID,
    deterministically hash it via uuid5 so any cluster ID or resource string is valid.
    """
    if not val:
        return uuid.uuid4()
    if isinstance(val, uuid.UUID):
        return val
    try:
        return uuid.UUID(str(val))
    except (ValueError, AttributeError):
        return uuid.uuid5(uuid.NAMESPACE_DNS, str(val))
