"""PPA CLI — Predictive Pod Autoscaler command-line interface."""

import os

# ── TensorFlow noise suppression ──
# MUST be set before any ML module is imported by the CLI
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
os.environ["TF_ENABLE_ONEDNN_OPTS"] = "0"

from ppa.cli.app import app  # noqa: E402

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "app",
]
