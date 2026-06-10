"""Tests for the top-level `ppa train` CLI command."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import tomllib
from typer.testing import CliRunner

from ppa.cli.app import app
from ppa.cli.commands.train import DEFAULT_TRAIN_HORIZONS, _resolve_training_horizons, train_cmd
from ppa.config import ARTIFACTS_DIR


def test_train_help_describes_multi_horizon_default():
    runner = CliRunner()

    result = runner.invoke(app, ["train", "--help"])

    assert result.exit_code == 0
    assert "rps_t3m, rps_t5m, and rps_t10m" in result.stdout
    assert "all models:" in result.stdout
    assert "rps_t10m" in result.stdout
    assert "[default: None]" not in result.stdout


def test_dev_extra_installs_training_dependencies():
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())
    dependencies = pyproject["project"]["dependencies"]
    extras = pyproject["project"]["optional-dependencies"]

    assert any(dep.startswith("prompt-toolkit>=") for dep in dependencies)
    for requirement in ("keras", "tensorflow", "scipy"):
        assert any(dep.startswith(f"{requirement}>=") for dep in extras["model"])
        assert any(dep.startswith(f"{requirement}>=") for dep in extras["dev"])


def test_resolve_training_horizons_defaults_to_rps_targets():
    assert _resolve_training_horizons(None) == DEFAULT_TRAIN_HORIZONS


def test_resolve_training_horizons_accepts_comma_separated_values():
    assert _resolve_training_horizons("rps_t3m,rps_t5m") == ["rps_t3m", "rps_t5m"]


def test_train_cmd_trains_all_default_horizons(monkeypatch, tmp_path):
    csv_path = tmp_path / "training.csv"
    csv_path.write_text("timestamp\n")

    calls: list[dict] = []

    fake_console = SimpleNamespace(print=lambda *args, **kwargs: None, is_terminal=True)
    monkeypatch.setattr("ppa.cli.commands.train.console", fake_console)
    monkeypatch.setattr(
        "ppa.cli.commands.train._build_training_progress_callback",
        lambda horizon: f"callback:{horizon}",
    )

    def fake_train_model(**kwargs):
        calls.append(kwargs)
        return {
            "metrics": {"val_mae": 0.12, "epochs_run": 3},
            "artifact_paths": {"model": Path(f"/tmp/{kwargs['target_col']}/ppa_model.keras")},
        }

    monkeypatch.setattr("ppa.cli.commands.train.os.path.exists", lambda path: True)
    monkeypatch.setitem(
        sys.modules,
        "ppa.model.train",
        SimpleNamespace(train_model=fake_train_model),
    )

    train_cmd(
        app="test-app",
        horizon=None,
        csv=str(csv_path),
        epochs=3,
        lookback=10,
        patience=2,
        eval_only=False,
        apply_after=False,
    )

    assert [call["target_col"] for call in calls] == DEFAULT_TRAIN_HORIZONS
    assert all(call["fit_verbose"] == 0 for call in calls)
    assert all(call["show_model_summary"] is False for call in calls)
    assert all(call["fit_callbacks"] == [f"callback:{call['target_col']}"] for call in calls)
    assert all(call["output_dir"] == str(ARTIFACTS_DIR) for call in calls)
