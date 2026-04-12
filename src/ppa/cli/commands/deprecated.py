"""ppa deprecated — Backward compatibility layer for v2 CLI.

Registers hidden Typer commands for old v1 names and forwards
them to their new v2 equivalents while printing a deprecation warning.
Spec Section 10: Deprecation Strategy.
"""

from __future__ import annotations

import typer

from ppa.cli.utils import warn


def _warn_deprecated(old: str, new: str) -> None:
    warn(f"'{old}' is deprecated.  Running '{new}' instead.")


def register_deprecated_commands(app: typer.Typer) -> None:
    """Register hidden placeholder commands that execute the new logic."""

    @app.command("startup", hidden=True, deprecated=True)
    def startup_compat(ctx: typer.Context) -> None:
        """Deprecated: Use ppa init."""
        _warn_deprecated("ppa startup", "ppa init")
        from ppa.cli.commands.init import init_cmd
        init_cmd(step=None, list_steps=False, dry_run=False, follow=False, app=None)

    @app.command("onboard", hidden=True, deprecated=True)
    def onboard_compat(
        app_name: str = typer.Option(..., "--app-name", "-a"),
        target: str = typer.Option(..., "--target", "-t"),
    ) -> None:
        """Deprecated: Use ppa add."""
        _warn_deprecated("ppa onboard", "ppa add")
        from ppa.cli.commands.add import add_cmd
        add_cmd(app_name=app_name, target=target, namespace="default", min_replicas=1, max_replicas=10, rps_capacity=20, safety_factor=1.15, scale_up=2.0, scale_down=1.0, dry_run=False)

    @app.command("deploy", hidden=True, deprecated=True)
    def deploy_compat(
        app_name: str = typer.Option("test-app", "--app-name", "-a"),
    ) -> None:
        """Deprecated: Use ppa apply."""
        _warn_deprecated("ppa deploy", "ppa apply")
        from ppa.cli.commands.apply import apply_cmd
        apply_cmd(app_name=app_name, namespace="default", dry_run=False, rollback=False, skip_build=False, keep_hpa=True, watch_after=False, yes=True)

    @app.command("monitor", hidden=True, deprecated=True)
    def monitor_compat() -> None:
        """Deprecated: Use ppa watch."""
        _warn_deprecated("ppa monitor", "ppa watch")
        from ppa.cli.commands.watch import watch_cmd
        watch_cmd(interval=15, app=None)

    @app.command("toolbox", hidden=True, deprecated=True)
    def toolbox_compat() -> None:
        """Deprecated: Use ppa debug."""
        warn("'ppa toolbox' is deprecated.  Use 'ppa debug' commands instead.")
        raise typer.Exit(1)
