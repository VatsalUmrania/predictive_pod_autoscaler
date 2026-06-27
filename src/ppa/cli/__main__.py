"""Entry point — invokes the Typer app exposing only `ppa model push`."""

from __future__ import annotations

import sys

from ppa.cli.app import app


def main() -> None:
    sys.exit(app())


if __name__ == "__main__":
    main()
