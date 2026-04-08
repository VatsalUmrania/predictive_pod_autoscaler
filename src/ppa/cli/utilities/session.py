"""Session management and progress tracking utilities for PPA CLI."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from typing import TYPE_CHECKING

from rich.console import Console
from rich.progress import (
    Progress,
    ProgressColumn,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.text import Text

from ppa.cli.utilities.common import console, heading, info, success, warn
from ppa.config import SESSION_FILE

if TYPE_CHECKING:
    import keras

__all__ = [
    "save_session",
    "load_session",
    "cleanup_session",
    "make_progress",
    "get_live_progress_callback",
]

# Session Management

def save_session(pids: dict[str, int]) -> None:
    """Persist background process PIDs and metadata to session file.

    Saves process information to a session file for later cleanup and tracking.
    Merges with existing session data if present, preserving start_time.

    Args:
        pids: Dictionary mapping process names to their PIDs (e.g., {"prometheus": 1234})

    Examples:
        >>> save_session({"grafana": os.getpid()})
        >>> session = load_session()
        >>> print(session["pids"]["grafana"])
        12345
    """
    try:
        data = load_session()
        data["updated_at"] = datetime.now().isoformat()
        if "start_time" not in data:
            data["start_time"] = data["updated_at"]

        if "pids" not in data:
            data["pids"] = {}

        data["pids"].update(pids)

        with open(SESSION_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        warn(f"Failed to save session: {e}")


def load_session() -> dict[str, object]:
    """Load session data from file with automatic format migration.

    Reads the session file and handles legacy formats where PIDs were stored
    as top-level keys. Automatically migrates old format to new structure.

    Returns:
        Dictionary with "pids" key mapping process names to PIDs, plus metadata
        keys like "start_time" and "updated_at"

    Examples:
        >>> session = load_session()
        >>> if session.get("pids"):
        ...     active_count = len(session["pids"])
        ...     print(f"{active_count} background processes running")
    """
    if not SESSION_FILE.exists():
        return {}
    try:
        with open(SESSION_FILE) as f:
            data = json.load(f)

        # Migration: if data is a flat dict of PIDs, move to "pids" key
        if data and "pids" not in data:
            # Filter out metadata keys if they somehow exist
            pids = {k: v for k, v in data.items() if k not in ["updated_at", "start_time"]}
            meta = {k: v for k, v in data.items() if k in ["updated_at", "start_time"]}
            data = {"pids": pids, **meta}

        return data  # type: ignore[no-any-return]
    except Exception:
        return {}


def cleanup_session() -> None:
    """Terminate all tracked background processes and remove session file.

    Sends SIGTERM to all processes tracked in the session file and removes
    the session file when complete. Handles Windows (taskkill) and Unix platforms.

    Logs success/warning for each process termination attempt.

    Examples:
        >>> cleanup_session()
        [step] Cleaning up PPA Session
        [success] Stopped prometheus
        [success] Stopped grafana
    """
    session = load_session()
    pids = session.get("pids", {})
    if not pids:
        info("No active PPA session found.")
        return

    heading("Cleaning up PPA Session")
    non_existent_pids = set()
    for name, pid in pids.items():  # type: ignore[attr-defined] # noqa: PERF203
        try:
            if sys.platform == "win32":
                result = subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True,
                    check=False,
                )
                if result.returncode == 0:
                    success(f"Stopped {name}")
                # returncode != 0 means process was already gone — ignore
            else:
                os.kill(pid, 0)  # Raises ProcessLookupError if already dead
                info(f"Stopping {name} (PID: {pid})...")
                os.kill(pid, signal.SIGTERM)
                success(f"Stopped {name}")
        except ProcessLookupError:  # noqa: PERF203
            non_existent_pids.add(name)
        except Exception as e:
            warn(f"Could not stop {name}: {e}")

    if SESSION_FILE.exists():
        SESSION_FILE.unlink()
    success("Session cleanup complete.")

# Progress Bar

def make_progress() -> Progress:
    """Create a styled Rich Progress bar instance.

    Generates a reusable Rich Progress object with PPA branding and styling.
    Use this for long-running operations to provide visual feedback.

    Returns:
        Rich Progress instance with spinners, bars, and timing

    Examples:
        >>> progress = make_progress()
        >>> with progress:
        ...     task_id = progress.add_task("[info]Loading...", total=100)
        ...     for i in range(100):
        ...         progress.advance(task_id, 1)
    """
    return Progress(
        SpinnerColumn(spinner_name="dots", style="brand"),
        TextColumn("[info]{task.description}[/info]"),
        ProgressColumn(),
        TaskProgressColumn(style="metric"),
        TimeElapsedColumn(style="dim"),
        console=console,
    )


def get_live_progress_callback(
    console_instance: Console | None = None,
) -> type[keras.callbacks.Callback]:
    """Return a Keras Callback that renders a Rich progress bar for training.

    Creates a custom Keras callback that displays training progress with Rich
    formatting, including loss, metrics, and ETA per epoch.

    Args:
        console_instance: Optional Rich console for output (uses default if not provided)

    Returns:
        keras.callbacks.Callback instance for use in model.fit()

    Examples:
        >>> callback = get_live_progress_callback()
        >>> model.fit(x, y, callbacks=[callback], epochs=10)
    """
    import keras
    from rich.console import Group
    from rich.live import Live

    cons = console_instance or console

    class LiveProgressCallback(keras.callbacks.Callback):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__()
            self.live = None
            self.progress = None
            self.epoch_start = 0
            self.last_completed_epoch = None
            self.latest_metrics = {}
            self.current_epoch = None

        def on_train_begin(self, logs=None):  # type: ignore[no-untyped-def] # noqa: ARG002
            """Handle training start event."""
            # Get total epochs and steps per epoch from Keras params
            self.total_epochs = self.params.get("epochs", 1)
            self.total_steps = self.params.get("steps", 1)

        def on_epoch_begin(self, epoch, logs=None):  # type: ignore[no-untyped-def] # noqa: ARG002
            """Handle epoch start event."""
            self.current_epoch = epoch + 1

            class BlockBarColumn(ProgressColumn):  # type: ignore[name-defined,misc]
                """Custom progress column with block characters."""

                def render(self, task):  # type: ignore[no-untyped-def]
                    """Render block-style progress bar."""
                    completed = int((task.percentage or 0) / 100 * 40)
                    remaining = 40 - completed
                    return Text("█" * completed + "░" * remaining, style="brand")

            self.epoch_start: float = time.time()
            self.progress = Progress(
                TextColumn("  "),
                BlockBarColumn(),
                TextColumn(" [progress.percentage]{task.percentage:>3.0f}% "),
                console=cons
            )
            self.task_id = self.progress.add_task("train", total=self.total_steps)

            self.header_text = Text(
                f"Epoch {epoch+1}/{self.total_epochs}", style="heading"
            )
            self.metrics_text = Text(
                "  loss: --- , mae: --- , ETA: ---", style="muted"
            )

            group = Group(
                self.header_text,
                self.progress,
                self.metrics_text,
                "",
            )

            self.live = Live(
                group, console=cons, transient=True, refresh_per_second=10
            )
            self.live.start()

        def on_batch_end(self, batch, logs=None):  # type: ignore[no-untyped-def]
            """Handle batch end event."""
            logs = logs or {}
            loss = logs.get('loss', 0.0)
            mae = logs.get('mae', 0.0)

            elapsed = time.time() - self.epoch_start
            steps_done = batch + 1
            time_per_step = elapsed / steps_done
            eta = int((self.total_steps - steps_done) * time_per_step)

            self.metrics_text.plain = (
                f"  loss: {loss:.4f} • mae: {mae:.4f} • ETA: {eta}s"
            )
            if self.progress:
                self.progress.update(self.task_id, completed=steps_done)

        def on_epoch_end(self, epoch, logs=None):  # type: ignore[no-untyped-def]
            """Handle epoch end event."""
            if self.live:
                from contextlib import suppress
                with suppress(Exception):
                    self.live.stop()

            logs = logs or {}
            self.latest_metrics = {
                "loss": logs.get('loss', 0.0),
                "mae": logs.get('mae', 0.0),
            }
            self.last_completed_epoch = epoch + 1

            loss = self.latest_metrics["loss"]
            mae = self.latest_metrics["mae"]
            duration = int(time.time() - self.epoch_start)

            cons.print(
                f"[success]✔[/success] Epoch {epoch+1}/{self.total_epochs} completed "
                f"— loss: {loss:.4f} — mae: {mae:.4f} — {duration}s"
            )

    return LiveProgressCallback()
