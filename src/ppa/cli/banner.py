"""PPA CLI banner — gradient text from purple to cyan.

Called once at session start or on --version.
Never called during command execution (spec Section 9).
"""

from __future__ import annotations

from rich.align import Align
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

console = Console()


def print_banner(version: str = "1.0.0") -> None:
    """Print the PPA CLI banner.

    Called once at interactive session start.
    Never called during command execution.
    """
    # Gradient: purple (#9F7AEA) → blue (#63B3ED) → cyan (#76E4F7)
    gradient_chars = [
        ("#9F7AEA", "P"),
        ("#9B7AEF", "P"),
        ("#8F7CF4", "A"),
        ("#7B80F7", " "),
        ("#6884F7", "C"),
        ("#5589F5", "L"),
        ("#4B90F2", "I"),
    ]

    # Build the title text with per-character colour
    title = Text()
    for color, char in gradient_chars:
        title.append(char, style=f"bold {color}")

    # Tagline in muted cyan
    tagline = Text(
        "Predictive Pod Autoscaler — ML-based Kubernetes scaling",
        style="dim cyan",
        justify="center",
    )

    # Version line
    version_text = Text(f"v{version}", style="dim", justify="center")

    # Compose into a panel
    content = Text.assemble(
        title,
        "\n",
        tagline,
        "\n",
        version_text,
    )
    content.justify = "center"

    panel = Panel(
        Align.center(content),
        border_style="dim",
        padding=(1, 4),
        expand=False,
    )

    console.print()
    console.print(Align.center(panel))
    console.print()


def print_banner_inline(version: str = "1.0.0") -> None:
    """Compact single-line banner for --version flag.

    Output: PPA v1.0.0 — Predictive Pod Autoscaler
    """
    text = Text()
    colors = ["#9F7AEA", "#8F7CF4", "#7B80F7"]
    for char, color in zip("PPA", colors, strict=False):
        text.append(char, style=f"bold {color}")

    text.append(f"  v{version}", style="dim")
    text.append("  —  Predictive Pod Autoscaler", style="dim cyan")

    console.print(text)
