from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import typer

from ppa.cli.commands import startup_steps


def test_start_port_forward_reports_success_when_process_survives(monkeypatch, tmp_path):
    process = MagicMock()
    process.poll.return_value = None

    popen = MagicMock(return_value=process)
    monkeypatch.setattr(startup_steps, "PORT_FORWARD_LOG_DIR", tmp_path)
    monkeypatch.setattr(startup_steps.subprocess, "Popen", popen)
    monkeypatch.setattr(startup_steps.time, "sleep", MagicMock())
    success = MagicMock()
    monkeypatch.setattr(startup_steps, "success", success)

    startup_steps._start_port_forward(["kubectl", "port-forward"], 9090, "Prometheus")

    popen.assert_called_once()
    assert popen.call_args.kwargs["start_new_session"] is True
    success.assert_called_once_with("Prometheus port-forward started (→ 9090)")


def test_start_port_forward_raises_with_log_detail_when_process_exits(monkeypatch, tmp_path):
    process = MagicMock()
    process.poll.return_value = 1

    def fake_popen(*args, **kwargs):
        kwargs["stderr"].write(b"unable to listen on port 9090\n")
        kwargs["stderr"].flush()
        return process

    monkeypatch.setattr(startup_steps, "PORT_FORWARD_LOG_DIR", tmp_path)
    monkeypatch.setattr(startup_steps.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(startup_steps.time, "sleep", MagicMock())
    error = MagicMock()
    monkeypatch.setattr(startup_steps, "error", error)

    with pytest.raises(typer.Exit):
        startup_steps._start_port_forward(["kubectl", "port-forward"], 9090, "Prometheus")

    error.assert_any_call(f"Prometheus port-forward failed (see {tmp_path / 'prometheus-9090.log'})")
    error.assert_any_call("unable to listen on port 9090")
