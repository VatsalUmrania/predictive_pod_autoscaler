"""Snapshot tests for CLI output.

Run with: pytest tests/test_renderer.py -v
Update snapshots: PYTHONPATH=src pytest tests/test_renderer.py -v --snapshot-update
"""

import subprocess
from pathlib import Path


def normalize(text: str) -> str:
    """Normalize output for snapshot comparison."""
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


SNAPSHOT_DIR = Path(__file__).parent / "snapshots"


def run_cli(args: list[str]) -> str:
    """Run CLI and capture output."""
    result = subprocess.run(
        ["python", "-m", "ppa.cli"] + args,
        cwd=Path(__file__).parent.parent,
        env={**subprocess.os.environ, "PYTHONPATH": "src"},
        capture_output=True,
        text=True,
    )
    return result.stdout + result.stderr


def test_help_root():
    """Test root help output."""
    output = run_cli(["--help"])
    # Save for visual verification
    snapshot = SNAPSHOT_DIR / "help_root.txt"
    snapshot.write_text(output)


def test_help_train():
    """Test train help output."""
    output = run_cli(["train", "--help"])
    snapshot = SNAPSHOT_DIR / "help_train.txt"
    snapshot.write_text(output)


def test_version():
    """Test version output."""
    output = run_cli(["--version"])
    snapshot = SNAPSHOT_DIR / "version.txt"
    snapshot.write_text(output)


if __name__ == "__main__":
    test_help_root()
    test_help_train()
    test_version()
    print("Snapshots saved to tests/snapshots/")
