"""
Utility functions and row formatters for TokenStore.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime
from typing import Any


def generate_approval_token(incident_id: str, action: str, secret: str = "") -> str:
    """Generate HMAC signature token for 1-click Slack interactive button payloads."""
    sec = secret or os.environ.get("SLACK_SIGNING_SECRET", "nexus_secret")
    msg = f"{incident_id}:{action}".encode()
    return hmac.new(sec.encode(), msg, hashlib.sha256).hexdigest()


def generate_token() -> str:
    """Generate a cryptographically secure SELFHEAL_TOKEN."""
    return "sh_" + secrets.token_urlsafe(32)


def format_app_row(r: dict[str, Any] | Any) -> dict[str, Any]:
    """Format a database row from app_tokens into a dictionary."""
    ca = (
        r["created_at"].isoformat()
        if isinstance(r["created_at"], datetime)
        else str(r["created_at"])
    )
    lu = (
        r["last_used"].isoformat()
        if isinstance(r["last_used"], datetime)
        else (str(r["last_used"]) if r["last_used"] else None)
    )
    tok = str(r["token"])
    return {
        "app_name": str(r["app_name"]),
        "tier": str(r["tier"]),
        "created_at": ca,
        "last_used": lu,
        "event_count": int(r["event_count"]),
        "token_prefix": tok[:8] + "…",
    }
