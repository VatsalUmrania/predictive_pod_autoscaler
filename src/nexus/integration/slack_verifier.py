"""
Slack request validation, webhook resolution, and action blocks.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

SLACK_INTERACTIVE_URL = os.environ.get("SLACK_INTERACTIVE_URL", "")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
SLACK_REQUEST_TOLERANCE_S = 60 * 5


def verify_slack_request(
    raw_body: bytes,
    headers: Any,
    *,
    signing_secret: str | None = None,
    tolerance_s: int | None = None,
) -> bool:
    """Validate a Slack request signature (HMAC-SHA256, v0 scheme)."""
    import nexus.integration.notifier as notifier_mod

    secret = (
        signing_secret
        if signing_secret is not None
        else getattr(
            notifier_mod,
            "SLACK_SIGNING_SECRET",
            os.environ.get("SLACK_SIGNING_SECRET", ""),
        )
    )
    if not secret:
        return False
    ts = headers.get("X-Slack-Request-Timestamp")
    sig = headers.get("X-Slack-Signature")
    if not ts or not sig:
        return False
    try:
        ts_int = int(ts)
    except (TypeError, ValueError):
        return False
    tol = tolerance_s if tolerance_s is not None else SLACK_REQUEST_TOLERANCE_S
    if abs(time.time() - ts_int) > tol:
        return False

    base = b"v0:" + str(ts).encode() + b":" + raw_body
    expected = "v0=" + hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, str(sig))


def resolve_slack_webhook(app_name: str | None) -> str | None:
    """Resolve the Slack webhook URL to use for app_name."""
    if app_name:
        try:
            from nexus.integration.dashboard import _policy_cache

            if app_name in _policy_cache:
                cfg = _policy_cache[app_name] or {}
                return (cfg.get("notifications") or {}).get("slack_webhook") or None
        except Exception:
            pass
    return os.environ.get("SLACK_WEBHOOK") or None


def approval_buttons_block(approval_id: str) -> dict[str, Any]:
    """Slack Block Kit action block with Approve/Reject buttons."""
    import nexus.integration.notifier as notifier_mod

    interactive_url = getattr(
        notifier_mod, "SLACK_INTERACTIVE_URL", SLACK_INTERACTIVE_URL
    )
    if not interactive_url:
        return {"type": "context", "elements": [{"type": "mrkdwn", "text": ""}]}
    return {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Approve"},
                "style": "primary",
                "action_id": "nexus_approve",
                "value": approval_id,
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Reject"},
                "style": "danger",
                "action_id": "nexus_reject",
                "value": approval_id,
            },
        ],
    }
