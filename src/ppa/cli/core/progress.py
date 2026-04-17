"""Unified progress indicators and spinners."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any

from rich.console import Group
from rich.live import Live
from rich.progress import ProgressBar
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

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

    def __enter__(self) -> Progress:
        """Start progress context manager."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Stop progress context manager."""
        pass

    def update(self, advance: float = 1, description: str | None = None) -> None:
        """Update progress.

        Args:
            advance: Number of steps to advance
            description: Optional new description
        """
        pass

    def set_total(self, total: float) -> None:
        """Set total steps (useful after creating with indeterminate progress)."""
        pass


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

# Training Progress Manager


class TrainingProgressManager:
    """Manages real-time training output with structured Live display."""

    def __init__(
        self,
        total_epochs: int = 50,
        use_colors: bool | None = None,
        target_col: str = "",
        app_name: str = "",
        lookback: int = 60,
        patience: int = 20,
    ):
        self.total_epochs = total_epochs
        self.current_epoch = 0
        self.current_batch = 0
        self.total_batches = 0
        self.patience_limit = patience
        self.patience_counter = 0
        self.use_colors = use_colors if use_colors is not None else self._is_tty()
        self.target_col = target_col
        self.app_name = app_name
        self.lookback = lookback

        # Timing
        self.start_time = time.time()
        self.epoch_start_time = time.time()

        # State
        self.data_info = {"rows": 0, "train_size": 0}
        self.model_info = {"total_params": 0}
        self.current_metrics = {"loss": 0.0, "mae": 0.0, "val_loss": 0.0}
        self.best_val_loss = float("inf")
        self.best_epoch = 0
        self.val_loss_trend = "—"
        self.epoch_metrics: list[dict[str, Any]] = []

        self.live: Live | None = None

    @staticmethod
    def _is_tty() -> bool:
        return sys.stdout.isatty()

    def _eta_str(self) -> str:
        if self.current_epoch == 0:
            return "--:--"
        elapsed = time.time() - self.start_time
        avg_epoch_time = elapsed / max(1, self.current_epoch)
        remaining_epochs = self.total_epochs - self.current_epoch
        remaining = avg_epoch_time * remaining_epochs

        if remaining < 60:
            return f"{int(remaining)}s"
        return f"{int(remaining // 60)}m {int(remaining % 60)}s"

    def _render(self) -> Group:
        """Render the current state as a Rich Group."""
        # 1. Header
        header = Text.assemble(
            ("  Training     ", "info"),
            ("·  ", "dim"),
            (f"{self.app_name}  ", "bold"),
            ("·  ", "dim"),
            (f"{self.target_col}  ", "bold"),
            ("·  ", "dim"),
            (f"{self.total_epochs} epochs", "info")
        )

        # 2. Info Line
        rows = self.data_info.get("rows", 0)
        params = self.model_info.get("total_params", 0)
        param_str = f"{params/1000:.1f}K" if params > 1000 else str(params)

        info_line = Text.assemble(
            ("  Dataset  ", "dim"),
            (f"{rows:,} samples  ", "bold"),
            ("·  ", "dim"),
            ("Model  ", "dim"),
            (f"{param_str} params  ", "bold"),
            ("·  ", "dim"),
            ("Lookback  ", "dim"),
            (f"{self.lookback} steps", "bold")
        )

        # 3. Epoch Line
        if self.total_batches > 0:
            pct = (self.current_batch / self.total_batches)
        else:
            pct = 0

        bar = ProgressBar(total=1.0, completed=pct, width=40)

        epoch_text = Text.assemble(
            (f"  Epoch {self.current_epoch:2d}/{self.total_epochs}  ", "bold"),
            " "
        )

        metrics_text = Text.assemble(
            (f"  {int(pct*100):3d}%  ", "dim"),
            ("loss: ", "dim"),
            (f"{self.current_metrics['loss']:.4f}  ", "info"),
            ("val: ", "dim"),
            (f"{self.current_metrics['val_loss']:.4f}  ", "info"),
            (f"{self.val_loss_trend}", "bold green" if self.val_loss_trend == "↓" else "bold red" if self.val_loss_trend == "↑" else "dim")
        )

        # Create a table for the epoch line to keep everything on one row
        epoch_table = Table.grid(padding=(0, 0))
        epoch_table.add_column()
        epoch_table.add_column()
        epoch_table.add_column()
        epoch_table.add_row(epoch_text, bar, metrics_text)

        # 4. Footer Line
        footer = Text.assemble(
            ("  Best epoch  ", "dim"),
            (f"{self.best_epoch}/{self.total_epochs}  ", "bold"),
            ("·  ", "dim"),
            ("best val loss  ", "dim"),
            (f"{self.best_val_loss:.6f}  ", "bold green" if self.best_val_loss != float("inf") else "dim"),
            ("·  ", "dim"),
            ("patience  ", "dim"),
            (f"{self.patience_counter}/{self.patience_limit}  ", "warning" if self.patience_counter > self.patience_limit // 2 else "dim"),
            ("·  ", "dim"),
            ("ETA  ", "dim"),
            (f"{self._eta_str()}", "bold")
        )

        sep = Text("  " + "─" * 76, style="dim")

        return Group(
            Text(""),
            header,
            sep,
            info_line,
            sep,
            epoch_table,
            sep,
            footer,
            sep
        )

    def on_data_loaded(self, rows: int, segments: int, train_size: int, val_size: int, test_size: int) -> None:
        self.data_info = {"rows": rows, "train_size": train_size}
        self.total_batches = (train_size + 31) // 32 # Assuming batch_size=32

        if not self.live:
            self.live = Live(self._render(), console=console, refresh_per_second=10)
            self.live.start()

    def on_model_created(self, total_params: int, trainable_params: int) -> None:
        self.model_info = {"total_params": total_params}
        if self.live:
            self.live.update(self._render())

    def on_batch_complete(self, batch: int, logs: dict[str, Any]) -> None:
        self.current_batch = batch
        self.current_metrics["loss"] = logs.get("loss", 0.0)
        self.current_metrics["mae"] = logs.get("mae", 0.0)
        if self.live:
            self.live.update(self._render())

    def on_epoch_complete(self, epoch: int, loss: float, val_loss: float, mae: float) -> None:
        self.current_epoch = epoch
        self.current_batch = self.total_batches
        self.current_metrics["val_loss"] = val_loss

        if val_loss < self.best_val_loss:
            if self.best_val_loss != float("inf"):
                self.val_loss_trend = "↓"
            self.best_val_loss = val_loss
            self.best_epoch = epoch
            self.patience_counter = 0
        else:
            self.val_loss_trend = "↑"
            self.patience_counter += 1

        if self.live:
            self.live.update(self._render())

        if epoch >= self.total_epochs:
            self.stop()

    def stop(self) -> None:
        if self.live:
            self.live.stop()
            self.live = None

    def get_callbacks(self) -> dict[str, Callable[..., None] | None]:
        return {
            "on_data_loaded": self.on_data_loaded,
            "on_model_created": self.on_model_created,
            "on_epoch_complete": self.on_epoch_complete,
            "on_batch_complete": self.on_batch_complete,
            "on_artifacts_saved": lambda paths: None,
        }

    def print_results_table(self, **kwargs) -> None:
        self.stop()
        pass

    def summary_table(self) -> str:
        return ""
