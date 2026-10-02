"""
Secret Detection Heuristics for Git Diff Scanning
"""

from __future__ import annotations

import re

_SECRET_PATTERNS: list[tuple[str, str]] = [
    (r'(?i)(api_key|api-key|apikey)\s*[=:]\s*["\'][a-zA-Z0-9_\-]{20,}["\']', "api_key"),
    (r'(?i)(secret|password|passwd|pwd)\s*[=:]\s*["\'][^"\']{8,}["\']', "password"),
    (r'(?i)(token)\s*[=:]\s*["\'][a-zA-Z0-9_\-\.]{20,}["\']', "token"),
    (r"AKIA[0-9A-Z]{16}", "aws_access_key"),
    (r"-----BEGIN (RSA|EC|DSA|OPENSSH) PRIVATE KEY-----", "private_key_pem"),
    (r"ghp_[a-zA-Z0-9]{36}", "github_token"),
    (r"sk-[a-zA-Z0-9]{48}", "openai_key"),
    (r"(?i)postgres://[^:]+:[^@]{8,}@", "db_connection_string"),
]

_SECRET_RES = [(re.compile(p), label) for p, label in _SECRET_PATTERNS]


def scan_diff_for_secrets(diff: str) -> list[dict]:
    """
    Scan a git diff for accidentally committed secrets.
    Only scans added lines (prefix '+').
    Returns list of {line_number, label, snippet} for each finding.
    """
    findings = []
    for i, line in enumerate(diff.splitlines(), 1):
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for regex, label in _SECRET_RES:
            if regex.search(line):
                findings.append(
                    {
                        "line_number": i,
                        "label": label,
                        "snippet": line[:100],
                    }
                )
    return findings
