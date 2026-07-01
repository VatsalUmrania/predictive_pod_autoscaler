"""Regression tests for CR conflict detection in the operator."""

import http.server
import importlib
import threading
from unittest.mock import patch

import prometheus_client

from ppa.domain import CRState


class DummyPatch:
    """Minimal patch object for operator unit tests."""

    def __init__(self) -> None:
        self.status: dict[str, object] = {}


def _base_spec(observer_mode: bool = False) -> dict[str, object]:
    return {
        "targetDeployment": "test-app",
        "maxReplicas": 10,
        "observerMode": observer_mode,
    }


def _load_operator_main():
    with (
        patch.object(http.server.HTTPServer, "__init__", return_value=None),
        patch.object(threading.Thread, "start", return_value=None),
        patch.object(prometheus_client, "start_http_server"),
    ):
        return importlib.import_module("ppa.operator.main")


class TestCRConflictWarnings:
    def setup_method(self):
        self.operator_main = _load_operator_main()
        self.original_state = self.operator_main._cr_state.copy()
        self.operator_main._cr_state.clear()

    def teardown_method(self):
        self.operator_main._cr_state.clear()
        self.operator_main._cr_state.update(self.original_state)

    def test_warning_is_not_emitted_for_different_target(self, monkeypatch):
        self.operator_main._cr_state[("default", "other-ppa")] = CRState(
            predictor=None,
            observer_mode=False,
            target_namespace="default",
            target_deployment="other-app",
        )

        monkeypatch.setattr(
            self.operator_main,
            "resolve_model_bundle",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                self.operator_main.BundlePendingError("pending")
            ),
        )

        with patch.object(self.operator_main.logger, "warning") as mock_warning:
            _config, state = self.operator_main._parse_crd_spec(
                _base_spec(),
                status={},
                meta={},
                cr_ns="default",
                cr_name="test-ppa",
                patch=DummyPatch(),
            )

        assert not any(
            "Multiple CRs detected" in call.args[0]
            for call in mock_warning.call_args_list
        )
        assert state.target_namespace == "default"
        assert state.target_deployment == "test-app"

    def test_warning_is_emitted_for_same_target_active_peer(self, monkeypatch):
        self.operator_main._cr_state[("default", "other-ppa")] = CRState(
            predictor=None,
            observer_mode=False,
            target_namespace="default",
            target_deployment="test-app",
        )

        monkeypatch.setattr(
            self.operator_main,
            "resolve_model_bundle",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                self.operator_main.BundlePendingError("pending")
            ),
        )

        with patch.object(self.operator_main.logger, "warning") as mock_warning:
            self.operator_main._parse_crd_spec(
                _base_spec(),
                status={},
                meta={},
                cr_ns="default",
                cr_name="test-ppa",
                patch=DummyPatch(),
            )

        assert any(
            "Multiple CRs detected" in call.args[0]
            for call in mock_warning.call_args_list
        )
