"""
NEXUS CLI
==========
Command-line interface for operators to interact with a running NEXUS instance.

All commands talk to the NEXUS Status API (default: http://localhost:8080).
Set NEXUS_API_URL to override.

Commands:
    nexus status                    — Full system snapshot
    nexus health                    — Quick liveness check
    nexus last-rca [--n N]          — Last N RCA decisions
    nexus runbooks                  — Runbook stats table
    nexus audit [--n N]             — Audit trail tail
    nexus approve ACTION_ID         — Approve a pending human action
    nexus approvals                 — List pending approval requests

    nexus prescale status           — Prescaler stats + recent decisions
    nexus prescale set-mode MODE    — shadow | advisory | autonomous

    nexus learning status           — Learning plane KPIs
    nexus learning run              — Trigger immediate feedback cycle
    nexus learning advisor          — RunbookAdvisor recommendations

Usage:
    # Run the NEXUS status API first:
    uvicorn nexus.observability.status_api:app --port 8080

    # Then in another terminal:
    nexus status
    nexus prescale set-mode advisory
    nexus learning run
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, Optional

import click

# ──────────────────────────────────────────────────────────────────────────────
# Rich / plain fallback
# ──────────────────────────────────────────────────────────────────────────────

try:
    from rich.console import Console
    from rich.table   import Table
    from rich.panel   import Panel
    from rich.syntax  import Syntax
    from rich         import box
    _RICH = True
    console = Console()
except ImportError:
    _RICH = False
    console = None  # type: ignore[assignment]


def _print(msg: str, style: str = "") -> None:
    if _RICH and console:
        console.print(msg, style=style)
    else:
        click.echo(msg)


def _error(msg: str) -> None:
    _print(f"[bold red]✗[/bold red] {msg}" if _RICH else f"ERROR: {msg}")
    sys.exit(1)


def _ok(msg: str) -> None:
    _print(f"[bold green]✓[/bold green] {msg}" if _RICH else f"OK: {msg}")


def _warn(msg: str) -> None:
    _print(f"[bold yellow]⚠[/bold yellow]  {msg}" if _RICH else f"WARN: {msg}")


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helper
# ──────────────────────────────────────────────────────────────────────────────

def _get(url: str, base: str) -> Dict[str, Any]:
    try:
        import httpx
        r = httpx.get(f"{base}{url}", timeout=10)
        r.raise_for_status()
        return r.json()
    except ImportError:
        _error("httpx not installed — run: pip install httpx")
    except Exception as exc:
        _error(f"API request failed ({base}{url}): {exc}")
    return {}


def _post(url: str, base: str, body: Optional[dict] = None) -> Dict[str, Any]:
    try:
        import httpx
        r = httpx.post(f"{base}{url}", json=body or {}, timeout=15)
        r.raise_for_status()
        return r.json()
    except ImportError:
        _error("httpx not installed — run: pip install httpx")
    except Exception as exc:
        _error(f"API request failed ({base}{url}): {exc}")
    return {}


def _fmt_float(v: Any, pct: bool = False) -> str:
    if v is None:
        return "—"
    f = float(v)
    return f"{f:.0%}" if pct else f"{f:.3f}"


def _severity_style(rate: float) -> str:
    """Return rich style for a false-heal / success rate."""
    if rate >= 0.85:
        return "bold green"
    if rate >= 0.60:
        return "bold yellow"
    return "bold red"


# ──────────────────────────────────────────────────────────────────────────────
# Root group
# ──────────────────────────────────────────────────────────────────────────────

@click.group()
@click.option(
    "--url",
    default=lambda: os.getenv("NEXUS_API_URL", "http://localhost:8080"),
    show_default=True,
    help="NEXUS Status API base URL.",
)
@click.pass_context
def cli(ctx: click.Context, url: str) -> None:
    """NEXUS Self-Healing Infrastructure CLI."""
    ctx.ensure_object(dict)
    ctx.obj["url"] = url.rstrip("/")


# ──────────────────────────────────────────────────────────────────────────────
# nexus health
# ──────────────────────────────────────────────────────────────────────────────

@cli.command()
@click.pass_context
def health(ctx: click.Context) -> None:
    """Quick liveness check — verifies the API is reachable."""
    data = _get("/health", ctx.obj["url"])
    _ok(
        f"NEXUS API is up — uptime={data.get('uptime_seconds', '?')}s  "
        f"ts={data.get('timestamp', '?')}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# nexus status
# ──────────────────────────────────────────────────────────────────────────────

@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Print a full NEXUS system status snapshot."""
    data = _get("/status", ctx.obj["url"])

    if _RICH:
        # Orchestrator section
        orc = data.get("orchestrator", {})
        if orc:
            t = Table(title="Orchestrator", box=box.ROUNDED, show_header=False)
            t.add_column("Key",   style="dim")
            t.add_column("Value", style="bold")
            for k, v in orc.items():
                t.add_row(k, str(v))
            console.print(t)

        # Prescaler section
        pre = data.get("prescaler", {})
        if pre:
            t = Table(title="Prescaler", box=box.ROUNDED, show_header=False)
            t.add_column("Key",   style="dim")
            t.add_column("Value", style="bold")
            for k, v in pre.items():
                t.add_row(k, str(v))
            console.print(t)

        # Learning section
        lrn = data.get("learning", {})
        if lrn:
            t = Table(title="Learning Plane", box=box.ROUNDED, show_header=False)
            t.add_column("Key",   style="dim")
            t.add_column("Value", style="bold")
            for k, v in lrn.items():
                t.add_row(k, str(v))
            console.print(t)
    else:
        click.echo(json.dumps(data, indent=2))


# ──────────────────────────────────────────────────────────────────────────────
# nexus last-rca
# ──────────────────────────────────────────────────────────────────────────────

@cli.command("last-rca")
@click.option("--n", default=5, show_default=True, help="Number of results to show.")
@click.pass_context
def last_rca(ctx: click.Context, n: int) -> None:
    """Show the N most recent RCA decisions from the Orchestrator."""
    data = _get(f"/rca/last?n={n}", ctx.obj["url"])

    if not data:
        _warn("No RCA results found (no incidents processed yet).")
        return

    if _RICH:
        t = Table(title=f"Last {n} RCA Decisions", box=box.MARKDOWN)
        t.add_column("Cluster ID",    style="dim",       no_wrap=True)
        t.add_column("Class",         style="cyan")
        t.add_column("Runbook",       style="magenta")
        t.add_column("Level",         style="bold")
        t.add_column("Confidence",    style="bold")
        t.add_column("RCA Source",    style="dim")
        t.add_column("Timestamp",     style="dim")

        for row in data:
            rca  = row.get("rca", {})
            conf = rca.get("confidence", 0)
            t.add_row(
                row.get("cluster_id", "?")[:8],
                rca.get("failure_class", "—"),
                rca.get("runbook_id") or "—",
                str(row.get("effective_level", "?")),
                _fmt_float(conf, pct=True),
                rca.get("source", "—"),
                (row.get("timestamp") or "")[:19],
            )
        console.print(t)
    else:
        click.echo(json.dumps(data, indent=2))


# ──────────────────────────────────────────────────────────────────────────────
# nexus runbooks
# ──────────────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--days", default=30, show_default=True, help="Lookback window in days.")
@click.pass_context
def runbooks(ctx: click.Context, days: int) -> None:
    """Show per-runbook healing statistics from the AuditTrail."""
    data = _get(f"/runbooks/stats?days={days}", ctx.obj["url"])

    if not data:
        _warn("No runbook stats — no healing actions in the specified window.")
        return

    if _RICH:
        t = Table(title=f"Runbook Stats (last {days} days)", box=box.ROUNDED)
        t.add_column("Runbook ID",     style="cyan",    no_wrap=True)
        t.add_column("Success Rate",   style="bold",    justify="right")
        t.add_column("Successes",      style="green",   justify="right")
        t.add_column("Failed",         style="red",     justify="right")
        t.add_column("Rolled Back",    style="yellow",  justify="right")
        t.add_column("Total",          style="dim",     justify="right")

        for rb_id, stats in sorted(data.items(), key=lambda x: -x[1].get("success_rate", 0)):
            rate   = stats.get("success_rate", 0)
            style  = _severity_style(rate)
            t.add_row(
                rb_id,
                f"[{style}]{rate:.0%}[/{style}]",
                str(stats.get("successes", 0)),
                str(stats.get("failures", 0)),
                str(stats.get("rolled_back", 0)),
                str(stats.get("total", 0)),
            )
        console.print(t)
    else:
        click.echo(json.dumps(data, indent=2))


# ──────────────────────────────────────────────────────────────────────────────
# nexus audit
# ──────────────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--n", default=15, show_default=True, help="Number of records to show.")
@click.pass_context
def audit(ctx: click.Context, n: int) -> None:
    """Tail the NEXUS AuditTrail."""
    data = _get(f"/audit/tail?n={n}", ctx.obj["url"])

    if not data:
        _warn("No audit records found.")
        return

    if _RICH:
        t = Table(title=f"Audit Trail (last {n})", box=box.MARKDOWN)
        t.add_column("Timestamp",  style="dim",     no_wrap=True)
        t.add_column("Runbook",    style="cyan",    no_wrap=True)
        t.add_column("Level",      style="bold",    justify="center")
        t.add_column("Target",     style="magenta")
        t.add_column("Outcome",    style="bold")

        OUTCOME_STYLE = {
            "success":    "bold green",
            "failed":     "bold red",
            "rolled_back":"bold yellow",
            "pending":    "dim",
        }
        for row in data:
            outcome = row.get("execution_outcome", "?")
            style   = OUTCOME_STYLE.get(outcome, "white")
            t.add_row(
                (row.get("timestamp") or "")[:19],
                row.get("runbook_id", "?"),
                str(row.get("healing_level", "?")),
                row.get("target") or "—",
                f"[{style}]{outcome}[/{style}]",
            )
        console.print(t)
    else:
        click.echo(json.dumps(data, indent=2))


# ──────────────────────────────────────────────────────────────────────────────
# nexus approve / nexus approvals
# ──────────────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("action_id")
@click.pass_context
def approve(ctx: click.Context, action_id: str) -> None:
    """Approve a pending human-review action (from Phase 3 governance queue)."""
    data = _post(f"/approve/{action_id}", ctx.obj["url"])
    _ok(f"Approved {action_id}: {data}")


@cli.command()
@click.pass_context
def approvals(ctx: click.Context) -> None:
    """List all actions currently awaiting human approval."""
    data = _get("/approvals/pending", ctx.obj["url"])
    if not data:
        _ok("No actions pending human approval.")
        return
    if _RICH:
        t = Table(title="Pending Approvals", box=box.ROUNDED)
        t.add_column("Action ID",  style="cyan")
        t.add_column("Runbook",    style="magenta")
        t.add_column("Target",     style="dim")
        t.add_column("Level",      style="bold")
        for row in data:
            t.add_row(
                row.get("action_id", "?"),
                row.get("runbook_id", "?"),
                row.get("target") or "—",
                str(row.get("healing_level", "?")),
            )
        console.print(t)
    else:
        click.echo(json.dumps(data, indent=2))


# ──────────────────────────────────────────────────────────────────────────────
# nexus prescale
# ──────────────────────────────────────────────────────────────────────────────

@cli.group()
def prescale() -> None:
    """Prescaler management (shadow → advisory → autonomous)."""


@prescale.command("status")
@click.pass_context
def prescale_status(ctx: click.Context) -> None:
    """Show prescaler statistics, mode, and recent decisions."""
    data  = _get("/prescaler", ctx.obj["url"])
    stats = data.get("stats", {})
    decs  = data.get("recent_decisions", [])

    if _RICH:
        t = Table(title="Prescaler Status", box=box.ROUNDED, show_header=False)
        t.add_column("Key",   style="dim")
        t.add_column("Value", style="bold")
        for k, v in stats.items():
            t.add_row(k, str(v))
        console.print(t)

        if decs:
            t2 = Table(title="Recent Prescale Decisions", box=box.MARKDOWN)
            t2.add_column("ID",         style="dim")
            t2.add_column("Deployment", style="cyan")
            t2.add_column("Replicas",   style="bold")
            t2.add_column("RPS",        style="magenta")
            t2.add_column("Confidence", style="bold")
            t2.add_column("Outcome",    style="dim")
            for d in decs:
                t2.add_row(
                    d.get("id", "?"),
                    d.get("deployment", "?"),
                    d.get("replicas", "?"),
                    d.get("rps", "?"),
                    _fmt_float(d.get("confidence"), pct=True),
                    d.get("outcome", "pending"),
                )
            console.print(t2)
    else:
        click.echo(json.dumps(data, indent=2))


@prescale.command("set-mode")
@click.argument("mode", type=click.Choice(["shadow", "advisory", "autonomous"]))
@click.pass_context
def prescale_set_mode(ctx: click.Context, mode: str) -> None:
    """Promote the Prescaler to a new autonomy mode."""
    data = _post(f"/prescaler/mode/{mode}", ctx.obj["url"])
    _ok(data.get("result", str(data)))


# ──────────────────────────────────────────────────────────────────────────────
# nexus learning
# ──────────────────────────────────────────────────────────────────────────────

@cli.group()
def learning() -> None:
    """Learning Plane management (feedback loop, KPIs, advisor)."""


@learning.command("status")
@click.pass_context
def learning_status(ctx: click.Context) -> None:
    """Show FeedbackLoop status and system KPIs."""
    data = _get("/learning", ctx.obj["url"])
    kpis = data.get("last_kpis") or {}

    if _RICH:
        t = Table(title="Learning Plane Status", box=box.ROUNDED, show_header=False)
        t.add_column("Key",   style="dim")
        t.add_column("Value", style="bold")
        for k, v in data.items():
            if k == "last_kpis":
                continue
            t.add_row(k, str(v))
        console.print(t)

        if kpis:
            t2 = Table(title="System KPIs", box=box.ROUNDED, show_header=False)
            t2.add_column("Metric", style="dim")
            t2.add_column("Value",  style="bold")
            for k, v in kpis.items():
                t2.add_row(k, str(v))
            console.print(t2)
    else:
        click.echo(json.dumps(data, indent=2))


@learning.command("run")
@click.pass_context
def learning_run(ctx: click.Context) -> None:
    """Trigger an immediate learning feedback cycle."""
    _ok("Triggering feedback cycle…")
    data = _post("/learning/run", ctx.obj["url"])
    _ok(f"Cycle complete — {data.get('cycles_run', '?')} cycles run")
    kpis = (data.get("last_kpis") or {})
    if kpis:
        _print(
            f"  success_rate={kpis.get('autonomous_success_rate', '?'):.0%}  "
            f"false_heal_rate={kpis.get('false_heal_rate', '?'):.0%}  "
            f"total_actions={kpis.get('total_actions', '?')}"
            if not _RICH else
            f"  [green]success_rate={kpis.get('autonomous_success_rate', 0):.0%}[/green]  "
            f"[red]false_heal_rate={kpis.get('false_heal_rate', 0):.0%}[/red]  "
            f"total_actions={kpis.get('total_actions', '?')}"
        )


@learning.command("advisor")
@click.pass_context
def learning_advisor(ctx: click.Context) -> None:
    """Show current RunbookAdvisor recommendations."""
    data = _get("/advisor", ctx.obj["url"])

    if not data:
        _ok("No recommendations — all runbooks look healthy!")
        return

    if _RICH:
        SEV_STYLE = {
            "action_required": "bold red",
            "warning":         "bold yellow",
            "info":            "bold cyan",
        }
        t = Table(title="Runbook Advisor Recommendations", box=box.ROUNDED)
        t.add_column("Severity",        style="bold", no_wrap=True)
        t.add_column("Runbook",         style="cyan")
        t.add_column("Recommendation",  style="magenta")
        t.add_column("Suggested Action",style="dim")

        for r in data:
            sev   = r.get("severity", "info")
            style = SEV_STYLE.get(sev, "white")
            t.add_row(
                f"[{style}]{sev}[/{style}]",
                r.get("runbook_id", "?"),
                r.get("recommendation", "?"),
                r.get("suggested_action", "?")[:60],
            )
        console.print(t)
    else:
        for r in data:
            click.echo(
                f"[{r.get('severity','?').upper()}] {r.get('runbook_id','?')}: "
                f"{r.get('recommendation','?')} — {r.get('suggested_action','?')}"
            )


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    cli(obj={})


if __name__ == "__main__":
    main()
