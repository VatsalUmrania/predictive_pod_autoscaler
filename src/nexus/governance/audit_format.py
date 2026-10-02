"""Row normalization and formatting for the Audit Trail."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any


def format_audit_row(row: Any) -> dict[str, Any]:
    """Normalize asyncpg record to standard dict with JSON objects and ISO timestamps."""
    d = dict(row)
    if "action_id" in d and d["action_id"] is not None:
        d["action_id"] = str(d["action_id"])
    if "timestamp" in d and isinstance(d["timestamp"], datetime):
        d["timestamp"] = d["timestamp"].isoformat()
    for col in ("pre_check_results", "post_check_results", "action_results"):
        if col in d and isinstance(d[col], str):
            try:
                d[col] = json.loads(d[col])
            except Exception:
                pass
    if "rollback_triggered" in d and d["rollback_triggered"] is not None:
        d["rollback_triggered"] = bool(d["rollback_triggered"])
    return d
