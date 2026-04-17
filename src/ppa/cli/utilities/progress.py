"""Unified progress indicators and spinners."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from rich.progress import (
    BarColumn,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
)
from rich.progress import (
    Progress as RichProgress,
)
from rich.spinner import Spinner

from ppa.cli.utils import console

# Progress wrapper

class Progress:
    """Wrapper around Rich progress with PPA theming."""

    def __init__(self, description: str = "Working", total: float | None = None):
        """Initialize progress tracker.

        Args:
            description: Description to display
            total: Total steps (None for indeterminate progress)
        """
        self.description = description
        self.total = total
        self._progress: RichProgress | None = None
        self._task_id: TaskID | None = None

    def __enter__(self) -> Progress:
        """Start progress context manager."""
        self._progress = RichProgress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn() if self.total else TextColumn(""),
            TimeRemainingColumn() if self.total else TextColumn(""),
            console=console,
        )
        self._progress.__enter__()
        self._task_id = self._progress.add_task(self.description, total=self.total)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Stop progress context manager."""
        if self._progress:
            self._progress.__exit__(exc_type, exc_val, exc_tb)

    def update(self, advance: float = 1, description: str | None = None) -> None:
        """Update progress.

        Args:
            advance: Number of steps to advance
            description: Optional new description
        """
        if self._progress and self._task_id is not None:
            self._progress.update(
                self._task_id,
                advance=advance,
                description=description or self.description,
            )

    def set_total(self, total: float) -> None:
        """Set total steps (useful after creating with indeterminate progress)."""
        if self._progress and self._task_id is not None:
            self._progress.update(self._task_id, total=total)

# Spinner context manager

@contextmanager
def spinner(text: str = "Working") -> Generator[None, None, None]:
    """Display a spinner while executing code block.

    Args:
        text: Text to display next to spinner

    Yields:
        None

    Example:
        >>> with spinner("Deploying operator..."):
        ...     time.sleep(2)  # Do some work
        # Output: ⠙ Deploying operator...
    """
    spinner_obj = Spinner("dots", text=text)
    with console.status(spinner_obj, spinner_style="progress.spinner"):
        yield
