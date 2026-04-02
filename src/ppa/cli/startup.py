"""Professional startup screen with getting started guide (similar to Claude/GitHub Copilot).

Displays:
- Welcome banner with PPA branding
- Quick start examples
- Feature highlights
- Common workflows
- Status indicators
"""

from __future__ import annotations

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ppa.cli.utils import console


def print_startup_screen() -> None:
    """Print formatted startup screen to console."""
    # Welcome header
    console.print()
    console.print(
        Text(
            "Predictive Pod Autoscaler",
            style="bold cyan",
            justify="center",
        )
    )
    console.print(
        Text(
            "Intelligent Kubernetes Scaling with ML-Powered Load Forecasting",
            style="dim cyan",
            justify="center",
        )
    )
    console.print()

    # Quick start examples table
    examples = Table(title="Quick Start Examples", show_header=False, box=None, padding=(0, 1))
    examples.add_column(style="green")
    examples.add_column(style="dim")

    examples.add_row("ppa startup --follow", "Bootstrap cluster & watch setup")
    examples.add_row("ppa status", "Check all components healthy")
    examples.add_row("ppa model train", "Train new ML model on metrics")
    examples.add_row("ppa deploy --dry-run", "Preview deployment changes")
    examples.add_row("ppa monitor", "Live HPA vs PPA comparison")
    examples.add_row("ppa onboard --app my-app", "Add new app to autoscaling")

    console.print(Panel(examples, border_style="cyan", padding=(1, 1)))
    console.print()

    # Feature highlights
    features = Table(title="Key Features", show_header=False, box=None, padding=(0, 1))
    features.add_column(style="yellow")
    features.add_column(style="dim", width=45)

    features.add_row("⚡ Predictive Scaling", "Forecast load 3-10 min ahead")
    features.add_row("🤖 ML Models", "LSTM networks trained on metrics")
    features.add_row("📊 Real-time Dashboard", "Live comparison with HPA")
    features.add_row("🔄 Continuous Learning", "Auto-retrain on new patterns")
    features.add_row("🛡️  Production-Ready", "Circuit breakers + graceful degradation")

    console.print(Panel(features, border_style="yellow", padding=(1, 1)))
    console.print()

    # Common workflows
    workflows = Table(title="Common Workflows", show_header=False, box=None, padding=(0, 1))
    workflows.add_column(style="magenta")
    workflows.add_column(style="dim", width=50)

    workflows.add_row("→ Setup", "ppa startup")
    workflows.add_row("→ Add App", "ppa onboard --app <app>")
    workflows.add_row("→ Train & Deploy", "ppa model train && ppa deploy")
    workflows.add_row("→ Check Health", "ppa status")
    workflows.add_row("→ Live Dashboard", "ppa monitor")
    workflows.add_row("→ Debug", "ppa toolbox | ppa follow")

    console.print(Panel(workflows, border_style="magenta", padding=(1, 1)))
    console.print()

    # Tips
    tip_text = Text("💡 Tip: ", style="dim", end="")
    console.print(tip_text, end="")
    console.print(Text("ppa <command> --help", style="bold cyan"), end="")
    console.print(Text(" for detailed help on any command.", style="dim"))
    console.print()


def print_command_guide(command_name: str, description: str, examples: list[tuple[str, str]], options: list[tuple[str, str]] | None = None) -> None:
    """Print a formatted guide for a specific command.

    Args:
        command_name: Name of the command (e.g., "startup", "deploy")
        description: One-line description
        examples: List of (command, description) tuples
        options: Optional list of (flag, description) tuples

    Example:
        >>> print_command_guide(
        ...     "startup",
        ...     "Bootstrap the cluster infrastructure",
        ...     [
        ...         ("ppa startup", "Interactive setup"),
        ...         ("ppa startup --follow", "Setup and watch progress"),
        ...     ],
        ...     [
        ...         ("--follow, -f", "Watch setup progress in real-time"),
        ...         ("--dry-run", "Show what would happen"),
        ...     ]
        ... )
    """
    # Title
    title = Text(f"ppa {command_name}", style="bold cyan")
    desc = Text(description, style="dim")
    console.print(Panel(title, border_style="cyan", padding=(0, 1)))
    console.print(desc)
    console.print()

    # Examples
    examples_table = Table(title="Examples", show_header=False, box=None, padding=(0, 1))
    examples_table.add_column(style="green")
    examples_table.add_column(style="dim")
    for cmd, help_text in examples:
        examples_table.add_row(f"$ {cmd}", help_text)
    console.print(examples_table)
    console.print()

    # Options
    if options:
        options_table = Table(title="Options", show_header=True, box=None, padding=(0, 1))
        options_table.add_column("Flag", style="magenta")
        options_table.add_column("Description", style="dim")
        for flag, help_text in options:
            options_table.add_row(flag, help_text)
        console.print(options_table)
