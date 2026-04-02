"""Enhanced help command guides for all PPA CLI commands.

Provides command-specific help with examples, common patterns, and troubleshooting.
"""

from __future__ import annotations

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ppa.cli.utils import console


def print_command_help(
    command: str,
    description: str,
    examples: list[tuple[str, str]],
    options: list[tuple[str, str]] | None = None,
    tips: list[str] | None = None,
) -> None:
    """Print comprehensive help for a CLI command.

    Args:
        command: Command name (e.g., "startup", "deploy")
        description: One-line description
        examples: List of (command, description) tuples
        options: Optional list of (flag, description) tuples
        tips: Optional list of helpful tips

    Example:
        >>> print_command_help(
        ...     "startup",
        ...     "Bootstrap the cluster infrastructure",
        ...     [
        ...         ("ppa startup", "Interactive setup"),
        ...         ("ppa startup --follow", "Setup and watch progress"),
        ...     ],
        ...     [
        ...         ("--follow, -f", "Watch setup progress in real-time"),
        ...     ],
        ...     ["Requires kubectl configured for your cluster"],
        ... )
    """
    # Title and description
    title_text = Text(f"ppa {command}", style="bold cyan")
    console.print(Panel(title_text, border_style="cyan", padding=(0, 1)))
    console.print(Text(description, style="dim"))
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
        console.print()

    # Tips
    if tips:
        console.print(Text("💡 Tips", style="bold yellow"))
        for tip in tips:
            console.print(f"  • {tip}", style="dim")
        console.print()


# ── Command-specific guides ──────────────────────────────────────────────────


def help_startup() -> None:
    """Help for 'ppa startup' command."""
    print_command_help(
        "startup",
        "Bootstrap the cluster infrastructure (replaces ppa_startup.sh)",
        [
            ("ppa startup", "Interactive setup with prompts"),
            ("ppa startup --follow", "Setup and watch progress"),
            ("ppa startup --skip-validation", "Skip pre-flight checks"),
            ("ppa startup --context kind", "Use specific kubecontext"),
        ],
        [
            ("--follow, -f", "Watch setup progress and follow logs"),
            ("--skip-validation", "Skip pre-flight checks (not recommended)"),
            ("--context <name>", "Use specific Kubernetes context"),
            ("--minikube-memory <MB>", "Set minikube memory (default: 4096)"),
            ("--no-grafana", "Skip Grafana dashboard setup"),
            ("--help", "Show detailed help"),
        ],
        [
            "Requires kubectl and Docker installed locally",
            "Creates local Kubernetes cluster (minikube or kind)",
            "Sets up Prometheus, Grafana, and operator",
            "Takes 5-10 minutes on first run",
            "Use --skip-validation only if you know what you're doing",
        ],
    )


def help_deploy() -> None:
    """Help for 'ppa deploy' command."""
    print_command_help(
        "deploy",
        "Deploy operator: train → convert → push → apply (replaces ppa_redeploy.sh)",
        [
            ("ppa deploy", "Full deployment (train + push + apply)"),
            ("ppa deploy --dry-run", "Preview changes without applying"),
            ("ppa deploy --skip-train", "Skip model training, use existing"),
            ("ppa deploy --image my-registry/ppa:v2", "Use custom operator image"),
        ],
        [
            ("--dry-run", "Show what would happen without applying"),
            ("--skip-train", "Use existing model, skip training"),
            ("--image <url>", "Use custom operator image"),
            ("--namespace <ns>", "Deploy to specific namespace"),
            ("--no-follow", "Don't watch logs after deployment"),
        ],
        [
            "Always use --dry-run first to review changes",
            "Training takes 5-15 minutes depending on data size",
            "Operator runs 1 replica by default (highly available setup coming)",
            "Previous operator version is replaced automatically",
        ],
    )


def help_monitor() -> None:
    """Help for 'ppa monitor' command."""
    print_command_help(
        "monitor",
        "Live dashboard — HPA vs PPA comparison with prediction validation",
        [
            ("ppa monitor", "Launch live monitoring dashboard"),
            ("ppa monitor --app my-app", "Focus on specific app"),
            ("ppa monitor --refresh 5", "Update every 5 seconds"),
        ],
        [
            ("--app <name>", "Monitor specific application"),
            ("--refresh <sec>", "Refresh interval in seconds (default: 3)"),
            ("--namespace <ns>", "Monitor specific namespace"),
        ],
        [
            "Dashboard updates in real-time from Prometheus",
            "Shows HPA vs PPA scaling decisions side-by-side",
            "Press 'q' to quit",
            "Requires Prometheus to be running",
        ],
    )


def help_onboard() -> None:
    """Help for 'ppa onboard' command."""
    print_command_help(
        "onboard",
        "Onboard a new application with PredictiveAutoscaler CRs",
        [
            ("ppa onboard --app my-app", "Interactive onboarding for my-app"),
            ("ppa onboard --app my-app --namespace prod", "Specify namespace"),
            ("ppa onboard --app my-app --dry-run", "Preview CRD"),
        ],
        [
            ("--app <name>", "Application name (required, must be DNS-1123)"),
            ("--namespace <ns>", "Kubernetes namespace (default: default)"),
            ("--replicas <n>", "Initial replicas (default: 2)"),
            ("--dry-run", "Show CRD without creating"),
        ],
        [
            "Creates PredictiveAutoscaler CustomResource for your app",
            "App must have Prometheus metrics available",
            "Requires labels: app=<name> on deployment",
            "Operator watches for new CRs and starts scaling automatically",
        ],
    )


def help_model() -> None:
    """Help for 'ppa model' command."""
    print_command_help(
        "model",
        "ML model — train, evaluate, convert, and run full pipeline",
        [
            ("ppa model train", "Train LSTM model on historical data"),
            ("ppa model evaluate", "Evaluate model accuracy"),
            ("ppa model convert", "Convert to TFLite format"),
            ("ppa model pipeline", "Full pipeline: train→eval→convert→promote"),
        ],
        [
            ("train", "Train new model (requires data in data/ directory)"),
            ("evaluate", "Test model accuracy on test split"),
            ("convert", "Convert to TFLite with quantization"),
            ("pipeline", "Run full pipeline with quality gates"),
            ("--epochs <n>", "Training epochs (default: 100)"),
            ("--batch-size <n>", "Batch size (default: 32)"),
        ],
        [
            "Models are LSTM networks with ~2-5MB TFLite footprint",
            "Training requires 5+ days of historical metrics",
            "Quantization validates 5% accuracy loss threshold",
            "Models auto-promote if they beat baseline by 10%",
        ],
    )


def help_status() -> None:
    """Help for 'ppa status' command."""
    print_command_help(
        "status",
        "Cluster status — health check for all PPA components",
        [
            ("ppa status", "Check all components"),
            ("ppa status --namespace ppa-system", "Check operator namespace"),
            ("ppa status --verbose", "Show detailed status"),
        ],
        [
            ("--namespace <ns>", "Check specific namespace"),
            ("--verbose, -v", "Show detailed diagnostics"),
            ("--json", "Output machine-readable JSON"),
        ],
        [
            "Checks: kubectl, Prometheus, operator pod, Grafana",
            "Returns exit code 0 if healthy, 1 if any component down",
            "Good for monitoring and CI/CD pipelines",
        ],
    )


def help_data() -> None:
    """Help for 'ppa data' command."""
    print_command_help(
        "data",
        "Data collection — export, validate, and inspect training data",
        [
            ("ppa data export", "Export metrics from Prometheus"),
            ("ppa data validate", "Validate exported data quality"),
            ("ppa data inspect", "Inspect data statistics and gaps"),
        ],
        [
            ("export", "Pull metrics from Prometheus (requires historical data)"),
            ("validate", "Check data completeness and quality"),
            ("inspect", "Show data summary and identify gaps"),
            ("--start <timestamp>", "Start time for export"),
            ("--end <timestamp>", "End time for export"),
        ],
        [
            "Data export requires 5+ days of historical metrics",
            "Validation catches NaN, infinite values, and gaps",
            "Inspect output helps optimize training configuration",
        ],
    )


def help_toolbox() -> None:
    """Help for 'ppa toolbox' command."""
    print_command_help(
        "toolbox",
        "Toolbox — utility and diagnostic commands for debugging",
        [
            ("ppa toolbox logs", "Tail operator logs"),
            ("ppa toolbox metrics", "Query Prometheus metrics"),
            ("ppa toolbox describe", "Describe PredictiveAutoscaler CR"),
        ],
        [
            ("logs", "Show operator pod logs with follow"),
            ("metrics", "Query Prometheus for specific metrics"),
            ("describe <app>", "Show PredictiveAutoscaler CR details"),
        ],
        [
            "Use for debugging operator issues",
            "Metrics query uses Prometheus PromQL",
            "Describe shows model status and prediction accuracy",
        ],
    )


def help_follow() -> None:
    """Help for 'ppa follow' command."""
    print_command_help(
        "follow",
        "Follow live logs and health (auto-cleanup on exit)",
        [
            ("ppa follow", "Follow all PPA logs"),
            ("ppa follow --app my-app", "Follow app-specific logs"),
            ("ppa follow --tail 100", "Show last 100 lines"),
        ],
        [
            ("--app <name>", "Follow specific app logs"),
            ("--tail <n>", "Show last n lines before following"),
            ("--namespace <ns>", "Follow specific namespace"),
        ],
        [
            "Press Ctrl+C to stop (auto-cleanup on exit)",
            "Streams from all components: operator, pods, metrics",
            "Useful for troubleshooting in real-time",
        ],
    )


def help_operator() -> None:
    """Help for 'ppa operator' command."""
    print_command_help(
        "operator",
        "Operator lifecycle — build, deploy, restart, and status",
        [
            ("ppa operator build", "Build operator Docker image"),
            ("ppa operator deploy", "Deploy to cluster"),
            ("ppa operator restart", "Restart operator pod"),
            ("ppa operator status", "Check operator health"),
        ],
        [
            ("build", "Build operator image from source"),
            ("deploy", "Deploy built image to cluster"),
            ("restart", "Restart operator pod (useful for debugging)"),
            ("status", "Show operator pod status"),
            ("--image <url>", "Use custom image (build only)"),
        ],
        [
            "Build creates Docker image (~150MB)",
            "Deploy pushes to registry and creates k8s deployment",
            "Restart forces reconciliation of all CRDs",
        ],
    )


# ── Help command dispatcher ──────────────────────────────────────────────────


COMMAND_HELP_MAP = {
    "startup": help_startup,
    "deploy": help_deploy,
    "monitor": help_monitor,
    "onboard": help_onboard,
    "model": help_model,
    "status": help_status,
    "data": help_data,
    "toolbox": help_toolbox,
    "follow": help_follow,
    "operator": help_operator,
}


def print_help_for_command(command: str) -> None:
    """Print help for a specific command.

    Args:
        command: Command name (e.g., "startup", "deploy")

    Example:
        >>> print_help_for_command("startup")
    """
    if command in COMMAND_HELP_MAP:
        COMMAND_HELP_MAP[command]()
    else:
        console.print(f"[error]Unknown command: {command}[/]")
        console.print(f"[dim]Available commands: {', '.join(sorted(COMMAND_HELP_MAP.keys()))}[/]")
