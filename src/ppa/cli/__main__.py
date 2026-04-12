"""PPA CLI entrypoint.

TF_CPP_MIN_LOG_LEVEL and TF_ENABLE_ONEDNN_OPTS are set BEFORE any import
so TensorFlow never pollutes stdout (spec Section 11, UX Principle 1).
"""

from __future__ import annotations

import os

# TensorFlow noise suppression — MUST be before any other import
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
os.environ["TF_ENABLE_ONEDNN_OPTS"] = "0"

import shlex
import sys

from prompt_toolkit import prompt
from prompt_toolkit.completion import NestedCompleter
from prompt_toolkit.history import InMemoryHistory

from ppa.cli.app import app
from ppa.cli.banner import print_banner
from ppa.cli.utils import console


def get_completer_dict() -> dict[str, object | None]:
    """Dynamically build a nested dictionary for auto-completion using Typer/Click internals."""
    from typer.main import get_command

    click_command = get_command(app)

    def _build_dict(cmd) -> dict[str, object | None]:
        d: dict[str, object | None] = {}
        # Add subcommands if it's a group
        if hasattr(cmd, "commands"):
            for sub_name, sub_cmd in cmd.commands.items():
                d[sub_name] = _build_dict(sub_cmd)

        # Add options/flags
        for param in cmd.params:
            for opt in param.opts:
                d[opt] = None
            for opt in param.secondary_opts:
                d[opt] = None

        return d

    completer_dict = _build_dict(click_command)

    # Add shell-specific commands
    completer_dict["exit"] = None
    completer_dict["quit"] = None

    return completer_dict


def run_interactive() -> None:
    """Run the PPA CLI in a persistent interactive loop with nested auto-completion."""
    from ppa import __version__

    print_banner(__version__)
    console.print("[cyan]PPA Interactive Shell[/] (Type 'exit' or Ctrl+C to quit)")
    console.print(
        "Try [bold]status[/], [bold]init --list[/], or [bold]watch[/]."
    )

    # Setup nested auto-completion
    completer = NestedCompleter.from_nested_dict(get_completer_dict())
    history = InMemoryHistory()

    while True:
        try:
            user_input = prompt(
                "ppa > ",
                completer=completer,
                history=history,
                complete_while_typing=True,
            )

            if not user_input.strip():
                continue

            clean_input = user_input.strip()
            if clean_input.lower() in ("exit", "quit"):
                console.print("[cyan]Goodbye![/]")
                break

            # Use shlex to correctly parse arguments
            try:
                args = shlex.split(clean_input)
            except ValueError as e:
                console.print(f"  [bold red]✗[/]  Parse error: {e}")
                continue

            # Execute the command
            try:
                app(args)
            except SystemExit:
                pass
            except Exception as e:
                console.print(f"  [bold red]✗[/]  Command error: {e}")

        except KeyboardInterrupt:
            console.print("\n[cyan]Exiting interactive mode...[/]")
            break
        except EOFError:
            console.print("\n[cyan]Exiting...[/]")
            break
        except Exception as e:
            console.print(f"  [bold red]✗[/]  Shell error: {e}")


def main() -> None:
    """Entry point for the PPA CLI (pyproject.toml console_scripts)."""
    if len(sys.argv) > 1:
        app()
    else:
        run_interactive()


if __name__ == "__main__":
    main()
