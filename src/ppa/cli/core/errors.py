"""Unified error handling with context and actionable messages."""

from __future__ import annotations


class PPAError(Exception):
    """Base exception for all PPA CLI errors."""

    def __init__(self, message: str, context: dict[str, str] | None = None, suggestion: str | None = None):
        """Initialize PPA error with message, context, and optional suggestion.

        Args:
            message: Human-readable error message
            context: Additional context (e.g., {"file": "config.yaml", "line": "42"})
            suggestion: Actionable suggestion to resolve the error
        """
        self.message = message
        self.context = context or {}
        self.suggestion = suggestion
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        """Format error with context and suggestion."""
        parts = [self.message]
        if self.context:
            parts.append("Context: " + ", ".join(f"{k}={v}" for k, v in self.context.items()))
        if self.suggestion:
            parts.append(f"Suggestion: {self.suggestion}")
        return "\n".join(parts)


class ValidationError(PPAError):
    """Raised when user input validation fails."""

    pass


class ConfigError(PPAError):
    """Raised when config file is invalid or missing."""

    pass


class KubernetesError(PPAError):
    """Raised when Kubernetes operations fail."""

    pass


class PrometheusError(PPAError):
    """Raised when Prometheus queries fail."""

    pass
