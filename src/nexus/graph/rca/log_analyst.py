"""
Log Analyst Agent for container logs and stack traces.
"""

from __future__ import annotations

import re
from typing import Any


class LogAnalystAgent:
    """Specialist agent analyzing container logs, stack traces, and exit codes."""

    def analyze(
        self, logs: list[str], raw_signals: list[dict[str, Any]]
    ) -> dict[str, Any]:
        findings: list[str] = []
        signatures: list[str] = []
        confidence_delta = 0.0

        combined = "\n".join(logs).lower()

        # Startup crashes / immediate exit codes / broken commands
        startup_crash_terms = (
            "crashing",
            "command failed",
            "fatal startup",
            "cannot execute",
            "command not found",
        )
        has_startup_crash = any(
            term in combined for term in startup_crash_terms
        ) or bool(re.search(r"\b(?:exit(?:ed with)? code 1|exit 1)\b", combined))
        if has_startup_crash:
            findings.append(
                "Container logs indicate immediate startup termination / exit code 1"
            )
            signatures.append("startup_crash")
            signatures.append("crashloop")
            confidence_delta += 0.30

        # OOM detection
        if any(
            term in combined
            for term in (
                "oomkilled",
                "fatal memory error",
                "out of memory",
                "heap out of memory",
                "exit code 137",
            )
        ):
            findings.append("Log indicates memory exhaustion / OOM kill")
            signatures.append("oom_event")
            confidence_delta += 0.35

        # Panics and unhandled exceptions
        if any(
            term in combined
            for term in (
                "panic:",
                "traceback (most recent call last)",
                "fatal error:",
                "nullpointerexception",
                "segmentation fault",
            )
        ):
            findings.append(
                "Log contains fatal application panic or unhandled exception trace"
            )
            signatures.append("runtime_panic")
            confidence_delta += 0.25

        # Connection / Database pool errors
        if any(
            term in combined
            for term in (
                "connection pool exhausted",
                "too many connections",
                "connection refused",
                "dial tcp: i/o timeout",
                "remaining connection slots",
            )
        ):
            findings.append(
                "Log indicates downstream connection pool or network socket exhaustion"
            )
            signatures.append("connection_exhaustion")
            confidence_delta += 0.25

        # Missing configuration / Env keys
        if any(
            term in combined
            for term in (
                "keyerror",
                "missing environment variable",
                "env var not set",
                "cannot find module",
            )
        ):
            findings.append(
                "Log indicates missing configuration or environment variable"
            )
            signatures.append("missing_config")
            confidence_delta += 0.30

        return {
            "agent": "LogAnalyst",
            "findings": findings,
            "signatures": signatures,
            "confidence_delta": confidence_delta,
            "has_critical_error": bool(signatures),
        }
