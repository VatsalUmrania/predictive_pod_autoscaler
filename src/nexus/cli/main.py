# ──────────────────────────────────────────────────────────────────────────────
# Rich / plain fallback
# ──────────────────────────────────────────────────────────────────────────────

try:
    from rich.console import Console
    _RICH = True
    console = Console()
except ImportError:
    _RICH = False
    console = None  # type: ignore[assignment]
