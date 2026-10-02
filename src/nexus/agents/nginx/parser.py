"""
NGINX Stub Status Parser
"""

from __future__ import annotations

import re

_STUB_STATUS_RE = re.compile(
    r"Active connections:\s*(\d+)\s+"
    r"server accepts handled requests\s+(\d+)\s+(\d+)\s+(\d+)\s+"
    r"Reading:\s*(\d+)\s+Writing:\s*(\d+)\s+Waiting:\s*(\d+)"
)


def parse_stub_status(text: str) -> dict | None:
    m = _STUB_STATUS_RE.search(text)
    if not m:
        return None
    return {
        "active_connections": int(m.group(1)),
        "accepts": int(m.group(2)),
        "handled": int(m.group(3)),
        "requests": int(m.group(4)),
        "reading": int(m.group(5)),
        "writing": int(m.group(6)),
        "waiting": int(m.group(7)),
    }
