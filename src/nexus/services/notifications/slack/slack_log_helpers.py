"""
Slack Block Kit helpers for YAML manifests and diagnostic logs.
"""

from __future__ import annotations

from typing import Any


def build_yaml_manifest_block(yaml_str: str) -> dict[str, Any]:
    """Format remediation YAML manifest into a Slack markdown code block."""
    cleaned = (yaml_str or "# No manifest provided").strip()
    if len(cleaned) > 2800:
        cleaned = cleaned[:2800] + "\n# ... (truncated)"
    return {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"*Proposed Remediation Manifest (YAML):*\n```yaml\n{cleaned}\n```",
        },
    }


def build_diagnostic_logs_block(logs: str) -> dict[str, Any]:
    """Format diagnostic pod logs into a Slack markdown code block."""
    cleaned = (logs or "No diagnostic logs captured.").strip()
    if len(cleaned) > 2800:
        cleaned = cleaned[:2800] + "\n... (truncated)"
    return {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"*Diagnostic Evidence & Pod Logs:*\n```text\n{cleaned}\n```",
        },
    }
