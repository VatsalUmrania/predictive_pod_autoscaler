from __future__ import annotations

import shlex
import sys

from prompt_toolkit import prompt
from prompt_toolkit.completion import NestedCompleter
from prompt_toolkit.history import InMemoryHistory

from cli.app import app
from cli.config import get_banner
from cli.utils import console


def get_completer_dict() -> dict:
    """Dynamically build a nested dictionary for auto-completion."""
    completer_dict = {}
    
    # 1. Add direct commands (e.g. status, monitor)
    for cmd in app.registered_commands:
        if cmd.name:
            completer_dict[cmd.name] = None

    # 2. Add command groups and their sub-commands
    for group in app.registered_groups:
        if group.name:
            sub_dict = {}
            # Access the Typer instance inside the group to find its commands
            if hasattr(group, "typer_instance") and group.typer_instance:
                for sub_cmd in group.typer_instance.registered_commands:
                    if sub_cmd.name:
                        sub_dict[sub_cmd.name] = None
            
            completer_dict[group.name] = sub_dict if sub_dict else None
    
    # 3. Add standard shell commands
    completer_dict["help"] = None
    completer_dict["exit"] = None
    completer_dict["quit"] = None
    
    return completer_dict


def run_interactive() -> None:
    """Run the PPA CLI in a persistent interactive loop with nested auto-completion."""
    console.print(get_banner())
    console.print("[bold cyan]PPA Interactive Shell[/bold cyan] (Type 'exit' or Ctrl+C to quit)")
    console.print("Try [bold]status[/bold], [bold]startup --list[/bold], or [bold]monitor[/bold].")

    # Setup nested auto-completion
    completer = NestedCompleter.from_nested_dict(get_completer_dict())
    history = InMemoryHistory()

    while True:
        try:
            # Use prompt_toolkit for completion and history
            # We use a simple prompt string because prompt_toolkit doesn't natively parse Rich markup
            # unless we use their HTML/ANSI formatting features, but keeping it simple for now.
            user_input = prompt(
                "ppa⚡ ",
                completer=completer,
                history=history,
                complete_while_typing=True,
            )
            
            if not user_input.strip():
                continue
            
            clean_input = user_input.strip()
            if clean_input.lower() in ["exit", "quit"]:
                console.print("[info]Goodbye![/info]")
                break
            
            # Use shlex to correctly parse arguments
            try:
                args = shlex.split(clean_input)
            except ValueError as e:
                console.print(f"[error]✘[/error] Parse error: {e}")
                continue
            
            # Execute the command
            try:
                app(args)
            except SystemExit:
                pass
            except Exception as e:
                console.print(f"[error]✘[/error] Command error: {e}")
                
        except KeyboardInterrupt:
            console.print("\n[info]Exiting interactive mode...[/info]")
            break
        except EOFError:
            console.print("\n[info]Exiting...[/info]")
            break
        except Exception as e:
            console.print(f"[error]✘[/error] Shell error: {e}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        app()
    else:
        run_interactive()
