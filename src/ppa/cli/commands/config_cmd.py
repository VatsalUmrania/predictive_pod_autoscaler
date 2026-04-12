"""ppa config — View and edit PPA configuration.

Provides access to the TOML configuration file for PPA.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer

from ppa.cli.utils import (
    console,
    error_block,
    info,
    success,
)

CONFIG_FILE = Path.home() / ".ppa" / "config.toml"


config_app = typer.Typer(rich_markup_mode="rich")


@config_app.command("get")
def config_get(
    key: str = typer.Argument(None, help="Specific config key to get. If omitted, shows all."),
) -> None:
    """Get the current configuration."""
    console.print()
    console.print("  [bold]PPA Configuration[/]")
    console.print(f"  [dim]Source: {CONFIG_FILE}[/]")
    console.print()

    if not CONFIG_FILE.exists():
        info("No custom config found. Using defaults.")
        raise typer.Exit()

    import tomlkit

    try:
        config_data = tomlkit.loads(CONFIG_FILE.read_text())
        if key:
            # Handle nested keys like ml.epochs
            parts = key.split(".")
            current = config_data
            for part in parts:
                if isinstance(current, dict) and part in current:
                    current = current[part]
                else:
                    error_block("Key not found", cause=f"Key '{key}' does not exist in config", fix="ppa config get")
                    raise typer.Exit(1)

            console.print(f"     [cyan]{key}[/] = {current}")
        else:
            # Print all
            for section, values in config_data.items():
                console.print(f"  [{section}]")
                if isinstance(values, dict):
                    for k, v in values.items():
                        console.print(f"     {k:<20} = {v}")
                console.print()
    except Exception as e:
        error_block("Failed to read config", cause=str(e), fix="ppa config edit")
        raise typer.Exit(1) from None


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Config key to set (e.g., ml.epochs)."),
    value: str = typer.Argument(..., help="Value to set."),
) -> None:
    """Set a configuration value."""
    import tomlkit

    if not CONFIG_FILE.exists():
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        config_data = tomlkit.document()
    else:
        try:
            config_data = tomlkit.loads(CONFIG_FILE.read_text())
        except Exception:
            config_data = tomlkit.document()

    parts = key.split(".")

    # Try to infer type
    typed_value: bool | int | float | str
    if value.lower() in ("true", "false"):
        typed_value = value.lower() == "true"
    elif value.isdigit():
        typed_value = int(value)
    else:
        try:
            typed_value = float(value)
        except ValueError:
            typed_value = value

    # Traverse/create
    current = config_data
    for part in parts[:-1]:
        if part not in current:
            current[part] = tomlkit.table()
        current = current[part]

    current[parts[-1]] = typed_value

    CONFIG_FILE.write_text(tomlkit.dumps(config_data))

    console.print()
    success(f"Config updated: [cyan]{key}[/] = {typed_value}")


@config_app.command("edit")
def config_edit() -> None:
    """Open the config file in the default system editor."""
    console.print()

    if not CONFIG_FILE.exists():
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.touch()
        info(f"Created new empty config file at {CONFIG_FILE}")

    editor = os.environ.get("EDITOR", "nano")

    info(f"Opening config file in [bold]{editor}[/]...")

    os.system(f"{editor} {CONFIG_FILE}")

    console.print()
    success("Configuration saved")
