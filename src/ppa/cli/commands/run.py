"""ppa run — Full lifecycle: init → add → train → apply.

New command that orchestrates the entire PPA setup pipeline.
Supports --from to resume from a specific phase on failure.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated

import typer

from ppa.cli.utils import (
    console,
    error_block,
    next_step,
    phase_header,
    success,
)
from ppa.config import DEFAULT_APP_NAME, DEFAULT_NAMESPACE


class Phase(str, Enum):
    init = "init"
    add = "add"
    train = "train"
    apply = "apply"


PHASES = [Phase.init, Phase.add, Phase.train, Phase.apply]


def run_cmd(
    app: Annotated[str | None, typer.Option("--app", "-a", help="Application name (used across all phases).")] = None,
    target: Annotated[str | None, typer.Option("--target", "-t", help="Target deployment name (defaults to --app).")] = None,
    from_phase: Annotated[Phase | None, typer.Option("--from", help="Resume from a specific phase (init, add, train, apply).")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Run each phase in dry-run mode.")] = False,
) -> None:
    """Run the full PPA lifecycle: init → add → train → apply.

    This is the one-command setup. Each phase is run sequentially.
    On failure, prints a resume command.

    \b
    EXAMPLES
      ppa run --app payments-api      # full setup end-to-end
      ppa run --from train            # resume from training
      ppa run --dry-run               # preview all phases

    \b
    REQUIRES
      All prerequisites for each phase (kubectl, Docker, etc.)
    """
    app_name = app or DEFAULT_APP_NAME
    target_name = target or app_name

    # Determine which phases to run
    if from_phase:
        start_idx = PHASES.index(from_phase)
    else:
        start_idx = 0

    phases_to_run = PHASES[start_idx:]
    total = len(PHASES)

    console.print()
    console.print(f"  Full lifecycle → [bold]{app_name}[/]")
    console.print()

    for i, phase in enumerate(phases_to_run, start=start_idx + 1):
        phase_header(i, total, phase.value)

        try:
            if phase == Phase.init:
                from ppa.cli.commands.init import init_cmd as _init_cmd

                # Run init without follow mode
                # Use programmatic call since we control the args
                _init_cmd(
                    step=None,
                    list_steps=False,
                    dry_run=dry_run,
                    follow=False,
                    app=None,
                )

            elif phase == Phase.add:
                from ppa.cli.commands.add import add_cmd as _add_cmd

                _add_cmd(
                    app_name=app_name,
                    target=target_name,
                    namespace=DEFAULT_NAMESPACE,
                    min_replicas=1,
                    max_replicas=10,
                    rps_capacity=20,
                    safety_factor=1.15,
                    scale_up=2.0,
                    scale_down=1.0,
                    dry_run=dry_run,
                )

            elif phase == Phase.train:
                from ppa.cli.commands.train import train_cmd as _train_cmd

                _train_cmd(
                    app=app_name,
                    horizon="rps_t10m",
                    csv=None,
                    epochs=50,
                    lookback=60,
                    patience=20,
                    eval_only=False,
                    apply_after=False,
                )

            elif phase == Phase.apply:
                from ppa.cli.commands.apply import apply_cmd as _apply_cmd

                _apply_cmd(
                    app_name=app_name,
                    namespace=DEFAULT_NAMESPACE,
                    dry_run=dry_run,
                    rollback=False,
                    skip_build=False,
                    keep_hpa=True,
                    watch_after=False,
                    yes=True,
                )

            success(f"{phase.value} complete")

        except SystemExit as e:
            if e.code and e.code != 0:
                error_block(
                    f"Pipeline stopped at {phase.value}",
                    cause=f"Phase {phase.value} exited with code {e.code}",
                    fix=f"ppa run --app {app_name} --from {phase.value}",
                )
                raise typer.Exit(1) from None
        except Exception as exc:
            error_block(
                f"Pipeline stopped at {phase.value}",
                cause=str(exc),
                fix=f"ppa run --app {app_name} --from {phase.value}",
            )
            raise typer.Exit(1) from None

    # All phases done
    console.print()
    success(f"PPA is live for {app_name}.  All phases completed.")
    next_step(f"ppa watch --app {app_name}", "observe live scaling")
