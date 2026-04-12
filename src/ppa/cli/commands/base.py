"""Base command class for all PPA CLI commands.

Provides shared initialization, validation, error handling, and output utilities.
All command modules should inherit from BaseCommand.
"""

from __future__ import annotations

import typer

from ppa.cli.core.config import CLIConfig, load_cli_config
from ppa.cli.utilities import PPAError, Progress, format_error_with_suggestion, spinner
from ppa.cli.utils import console, error, info, success, warn


class BaseCommand:
    """Base class for all PPA CLI command groups.

    Provides:
    - Centralized config loading and validation
    - Consistent error handling with suggestions
    - Unified progress/spinner utilities
    - Output styling (success, warning, error, info)
    - Pre-flight validation hooks

    Subclasses should override:
    - _validate(): Pre-flight checks (e.g., kubectl, Prometheus connectivity)
    - Commands can call self.config, self.error(), self.success(), etc.

    Example:
        >>> class MyCommand(BaseCommand):
        ...     def _validate(self) -> None:
        ...         self.validate_kubernetes()
        ...
        ...     @app.command()
        ...     def my_command(self):
        ...         self.success("Done!")
    """

    def __init__(self):
        """Initialize base command with config."""
        self.config = self._load_config()
        self._validate()

    def _load_config(self) -> CLIConfig:
        """Load CLI config, handling errors gracefully."""
        try:
            return load_cli_config()
        except Exception as e:
            warn(f"Could not load config: {e}")
            return CLIConfig()

    def _validate(self) -> None:
        """Pre-flight validation hook. Override in subclasses."""
        pass

    # Output helpers

    def success(self, msg: str) -> None:
        """Print success message."""
        success(msg)

    def error(self, msg: str, context: dict[str, str] | None = None) -> None:
        """Print error message with optional context.

        Args:
            msg: Error message
            context: Optional context dict (displayed as key=value pairs)
        """
        if context:
            context_str = " | ".join(f"{k}={v}" for k, v in context.items())
            error(f"{msg} ({context_str})")
        else:
            error(msg)

    def warn(self, msg: str) -> None:
        """Print warning message."""
        warn(msg)

    def info(self, msg: str) -> None:
        """Print info message."""
        info(msg)

    # Validation helpers

    def validate_kubernetes(self) -> None:
        """Validate kubectl can connect to cluster."""
        from ppa.cli.utilities import validate_kubernetes_connection

        try:
            with spinner("Checking Kubernetes connectivity..."):
                validate_kubernetes_connection(self.config.kubeconfig)
            self.success("Kubernetes cluster accessible")
        except Exception as e:
            self.error(f"Kubernetes validation failed: {e}")
            raise

    def validate_prometheus(self) -> None:
        """Validate Prometheus is accessible."""
        from ppa.cli.utilities import validate_prometheus_connection

        try:
            with spinner("Checking Prometheus connectivity..."):
                validate_prometheus_connection(self.config.prometheus_url)
            self.success(f"Prometheus accessible at {self.config.prometheus_url}")
        except Exception as e:
            self.error(f"Prometheus validation failed: {e}")
            raise

    # Progress helpers

    def progress(self, description: str = "Working", total: float | None = None) -> Progress:
        """Create progress tracker.

        Args:
            description: Description to display
            total: Total steps (None for indeterminate)

        Returns:
            Progress context manager

        Example:
            >>> with self.progress("Processing items", total=100) as p:
            ...     for item in items:
            ...         process(item)
            ...         p.update()
        """
        return Progress(description, total)

    def spinner(self, text: str = "Working"):
        """Context manager for spinner.

        Args:
            text: Text to display with spinner

        Example:
            >>> with self.spinner("Deploying..."):
            ...     deploy()
        """
        return spinner(text)

    # Error handling

    def handle_error(self, exc: Exception, user_input: str | None = None) -> None:
        """Handle error with optional suggestion.

        Args:
            exc: Exception to handle
            user_input: User input that caused error (for suggestions)

        Example:
            >>> try:
            ...     do_something()
            ... except ValueError as e:
            ...     self.handle_error(e, "my-app")
        """
        if isinstance(exc, PPAError):
            msg = str(exc)
        else:
            msg = str(exc)

        if user_input:
            msg = format_error_with_suggestion(msg, user_input)

        console.print(msg, style="error")

    def exit_error(self, msg: str, code: int = 1) -> None:
        """Print error and exit with code."""
        self.error(msg)
        raise typer.Exit(code=code)
