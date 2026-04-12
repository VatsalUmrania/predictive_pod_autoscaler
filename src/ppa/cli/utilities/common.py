"""Core CLI utilities: console, styling, subprocess wrappers, and output formatting."""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

from rich.console import Console
from rich.table import Table

from ppa.config import PPA_THEME

if TYPE_CHECKING:
    pass

__all__ = [
    "console",
    "success",
    "warn",
    "error",
    "info",
    "abort",
    "done",
    "heading",
    "step_heading",
    "run_cmd",
    "run_cmd_silent",
    "kubectl",
    "check_binary",
    "wait_for_pods",
    "build_kv_table",
]

# Singleton Console
console = Console(theme=PPA_THEME, highlight=False)

# Styled Output Helpers


def success(msg: str) -> None:
    """Print a success message with green checkmark."""
    console.print(f"  [success]✓[/success] {msg}")


def warn(msg: str) -> None:
    """Print a warning message with yellow warning icon."""
    console.print(f"  [warning]⚠[/warning] {msg}")


def error(msg: str, hint: str | None = None, cmd: str | None = None) -> None:
    """Print an error message with optional hint and command suggestion.

    Args:
        msg: Error message to display
        hint: Optional helpful hint for resolving the issue
        cmd: Optional command to suggest trying
    """
    console.print(f"  [error]✗[/error] {msg}")
    if hint:
        console.print(f"    [muted]Hint: {hint}[/muted]")
    if cmd:
        console.print(f"    [dim]Try:[/dim] [cmd]{cmd}[/cmd]")


def info(msg: str) -> None:
    """Print an info message with blue info icon."""
    console.print(f"  [info]ℹ[/info] {msg}")


def abort(msg: str, hint: str | None = None, code: int = 1, cmd: str | None = None) -> None:
    """Print an error and exit immediately.

    Args:
        msg: Error message to display
        hint: Optional helpful hint
        code: Exit code (default: 1)
        cmd: Optional command suggestion
    """
    error(msg, hint=hint, cmd=cmd)
    sys.exit(code)


def done(msg: str = "Done", elapsed: float | None = None) -> None:
    """Print a success completion state with optional timing.

    Args:
        msg: Completion message (default: "Done")
        elapsed: Optional elapsed time in seconds to display
    """
    suffix = f" [timing]({elapsed:.1f}s)[/timing]" if elapsed is not None else ""
    console.print(f"  [success]✓[/success] {msg}{suffix}")


def heading(title: str) -> None:
    """Print a section heading with a horizontal rule.

    Args:
        title: The section title to display
    """
    console.print()
    console.rule(f"[heading]{title}[/heading]", style="brand")


def step_heading(step_num: int, total: int, title: str) -> None:
    """Print an inline step indicator.

    Args:
        step_num: Current step number
        total: Total number of steps
        title: Step title
    """
    console.print()
    console.print(f"  [step][{step_num}/{total}][/step] [heading]{title}[/heading]")

# Subprocess Helpers

def run_cmd(
    cmd: list[str] | str,
    title: str = "Running",
    *,
    capture: bool = False,
    check: bool = True,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    shell: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess with a Rich spinner and error handling.

    Executes a command with visual feedback via a spinner. On failure, displays
    contextual error hints (connection issues, missing files, permissions, etc.)
    before exiting if check=True.

    Args:
        cmd: Command to execute as list of strings or single string
        title: Spinner status message
        capture: If True, return captured stdout/stderr; else stream to console
        check: If True, exit on non-zero return code; else return result
        cwd: Working directory for command execution
        env: Environment variables (merged with os.environ)
        shell: If True, run command through shell

    Returns:
        subprocess.CompletedProcess with exit code, stdout, and stderr

    Raises:
        SystemExit: If check=True and command returns non-zero exit code

    Examples:
        >>> result = run_cmd(["echo", "hello"], capture=True)
        >>> result.stdout
        'hello\\n'

        >>> run_cmd("make build", shell=True, title="Building")
    """
    with console.status(f"[info]{title}...[/info]", spinner="dots"):
        result = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            check=False,
            cwd=cwd,
            env=env,
            shell=shell,
        )

    if check and result.returncode != 0:
        cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
        error_msg = f"Command failed (exit {result.returncode}): {cmd_str}"
        hint = None
        stderr_lower = result.stderr.lower() if result.stderr else ""
        if "connection refused" in stderr_lower:
            hint = "Is the related service running? Try checking 'ppa status'."
        elif "not found" in stderr_lower or "no such file" in stderr_lower:
            hint = "Check if the path or resource exists."
        elif "permission denied" in stderr_lower:
            hint = "Try running with elevated privileges or check file ownership."
        elif "timeout" in stderr_lower:
            hint = "The operation timed out. Check connectivity."

        error(error_msg, hint=hint)
        if result.stderr:
            console.print(f"    [dim]{result.stderr.strip()[:500]}[/dim]")
        sys.exit(result.returncode)

    return result


def run_cmd_silent(
    cmd: list[str] | str,
    *,
    check: bool = True,
    cwd: str | None = None,
    shell: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess silently with error handling.

    Executes a command with all output captured and no visual feedback.
    Useful for background operations or when capture is critical.

    Args:
        cmd: Command to execute as list of strings or single string
        check: If True, exit on non-zero return code
        cwd: Working directory for command execution
        shell: If True, run command through shell

    Returns:
        subprocess.CompletedProcess with exit code, stdout, and stderr

    Raises:
        SystemExit: If check=True and command returns non-zero exit code

    Examples:
        >>> result = run_cmd_silent(["git", "status"])
        >>> if result.returncode == 0:
        ...     print("Git is working")
    """
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
        shell=shell,
    )
    if check and result.returncode != 0:
        error_msg = f"Command failed: {cmd if isinstance(cmd, str) else ' '.join(cmd)}"
        hint = None
        stderr_lower = result.stderr.lower() if result.stderr else ""
        if "connection refused" in stderr_lower:
            hint = "Is the related service running? Try checking 'ppa status'."
        elif "not found" in stderr_lower or "no such file" in stderr_lower:
            hint = "Check if the path or resource exists."
        elif "permission denied" in stderr_lower:
            hint = "Try running with elevated privileges or check file ownership."
        elif "timeout" in stderr_lower:
            hint = "The operation timed out. Check connectivity."

        error(error_msg, hint=hint)
        if result.stderr:
            console.print(f"    [dim]{result.stderr.strip()[:300]}[/dim]")
        sys.exit(result.returncode)
    return result


def kubectl(
    *args: str,
    namespace: str | None = None,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Execute a kubectl command with optional namespace and error handling.

    Convenience wrapper around kubectl that manages namespace specifications
    and provides consistent error handling across all Kubernetes commands.

    Args:
        *args: kubectl subcommands and arguments (e.g., "get", "pods")
        namespace: Target Kubernetes namespace (added via -n flag if provided)
        check: If True, exit on non-zero return code
        capture: If True, capture output silently; else stream to console

    Returns:
        subprocess.CompletedProcess with exit code, stdout, and stderr

    Raises:
        SystemExit: If check=True and kubectl returns non-zero exit code

    Examples:
        >>> result = kubectl("get", "pods", namespace="default", capture=True)
        >>> kubectl("apply", "-f", "manifest.yaml", namespace="kube-system")
        >>> kubectl("delete", "pod", "my-pod", namespace="monitoring")
    """
    cmd = ["kubectl"]
    if namespace:
        cmd += ["-n", namespace]
    cmd += list(args)
    return (
        run_cmd_silent(cmd, check=check)
        if capture
        else run_cmd(cmd, title=f"kubectl {args[0]}", check=check)
    )


def check_binary(name: str) -> bool:
    """Check if a binary is available on system PATH.

    Searches the system PATH for an executable with the given name.
    Useful for pre-flight checks of required tools.

    Args:
        name: Binary/executable name to search for (e.g., "kubectl", "docker")

    Returns:
        True if binary exists on PATH, False otherwise

    Examples:
        >>> if not check_binary("kubectl"):
        ...     abort("kubectl is not installed. Please install it first.")
    """
    return shutil.which(name) is not None


def wait_for_pods(
    label: str,
    namespace: str = "default",
    timeout: int = 120,
) -> bool:
    """Wait for pods matching a label selector to reach ready condition.

    Polls Kubernetes until pods matching the given label are in ready state,
    with a maximum wait time of `timeout` seconds.

    Args:
        label: Kubernetes label selector (e.g., "app=my-app")
        namespace: Kubernetes namespace to monitor (default: "default")
        timeout: Maximum wait time in seconds (default: 120)

    Returns:
        True if pods reached ready state, False if timeout exceeded

    Examples:
        >>> if wait_for_pods("app=operator", namespace="ppa-system"):
        ...     print("Operator is ready")
        ... else:
        ...     print("Operator failed to start")
    """
    info(f"Waiting for pods: {label} (ns={namespace})")
    result = run_cmd(
        [
            "kubectl",
            "wait",
            "--for=condition=ready",
            "pod",
            "-l",
            label,
            "-n",
            namespace,
            f"--timeout={timeout}s",
        ],
        title=f"Waiting for {label}",
        check=False,
        capture=True,
    )
    if result.returncode == 0:
        success("Pods ready")
        return True
    warn("Timeout — check pods manually")
    return False

# Table Builder

def build_kv_table(
    title: str,
    data: dict[str, str],
    key_style: str = "info",
    val_style: str = "",
) -> Table:
    """Create a styled Rich table for key-value data display.

    Generates a simple two-column key-value table with customizable styling
    for presentation in CLI output.

    Args:
        title: Table title displayed above the data
        data: Dictionary of key-value pairs to display
        key_style: Rich style for key column (default: "info")
        val_style: Rich style for value column (default: no style)

    Returns:
        Rich Table object ready for printing via console.print()

    Examples:
        >>> table = build_kv_table(
        ...     "Pod Status",
        ...     {"Namespace": "default", "Name": "my-pod"},
        ...     key_style="cyan",
        ...     val_style="green"
        ... )
        >>> console.print(table)
    """
    table = Table(title=title, show_header=False, border_style="info", padding=(0, 2))
    table.add_column("Key", style=key_style, min_width=20)
    table.add_column("Value", style=val_style)
    for k, v in data.items():
        table.add_row(k, str(v))
    return table
