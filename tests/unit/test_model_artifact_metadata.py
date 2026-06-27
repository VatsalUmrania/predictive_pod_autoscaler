from __future__ import annotations

import json
from types import SimpleNamespace

from ppa.cli.commands import push


def test_promote_artifacts_copies_metadata(tmp_path):
    from ppa.model.pipeline import promote_artifacts

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()

    # Canonical filenames (no horizon suffix)
    tflite_path = artifacts_dir / "ppa_model.tflite"
    scaler_path = artifacts_dir / "scaler.pkl"
    target_scaler_path = artifacts_dir / "target_scaler.pkl"
    summary_path = artifacts_dir / "eval_summary.json"
    metadata_path = artifacts_dir / "ppa_model_metadata.json"

    tflite_path.write_bytes(b"TFL3")
    scaler_path.write_bytes(b"scaler")
    target_scaler_path.write_bytes(b"target-scaler")
    summary_path.write_text("{}")
    metadata_payload = {"feature_columns": ["a"], "lookback": 60}
    metadata_path.write_text(json.dumps(metadata_payload))

    champion_dir = tmp_path / "champions"
    promoted_paths = promote_artifacts(
        app_name="test-app",
        target="rps_t10m",
        challenger_paths={
            "tflite": str(tflite_path),
            "scaler": str(scaler_path),
            "target_scaler": str(target_scaler_path),
        },
        eval_summary_path=str(summary_path),
        champion_dir=str(champion_dir),
    )

    promoted_dir = champion_dir / "test-app" / "rps_t10m"
    assert promoted_paths["model"] == str(promoted_dir / "ppa_model.tflite")
    assert (promoted_dir / "ppa_model.tflite").exists()
    assert (promoted_dir / "scaler.pkl").exists()
    assert (promoted_dir / "target_scaler.pkl").exists()
    assert (promoted_dir / "ppa_model_metadata.json").exists()
    assert (
        json.loads((promoted_dir / "ppa_model_metadata.json").read_text())
        == metadata_payload
    )


def test_push_models_copies_metadata_to_horizon_path(tmp_path, monkeypatch):
    champion_root = tmp_path / "champions"
    model_dir = champion_root / "test-app" / "rps_t10m"
    model_dir.mkdir(parents=True)

    (model_dir / "ppa_model.tflite").write_bytes(b"TFL3")
    metadata_payload = {"feature_columns": ["a"], "lookback": 60}
    (model_dir / "ppa_model_metadata.json").write_text(json.dumps(metadata_payload))

    csv_path = tmp_path / "training.csv"
    csv_path.write_text("timestamp,rps_t10m\n2026-04-09T00:00:00Z,1\n")

    copy_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(push, "CHAMPION_DIR", champion_root)
    monkeypatch.setattr(push, "PROJECT_DIR", tmp_path / "project")
    monkeypatch.setattr(push, "validate_cluster", lambda: True)
    monkeypatch.setattr(push, "ensure_exists", lambda *args, **kwargs: True)
    monkeypatch.setattr(push, "create_loader_pod", lambda *args, **kwargs: None)
    monkeypatch.setattr(push, "wait_for_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(push, "delete_pod", lambda *args, **kwargs: None)
    monkeypatch.setattr(push, "mkdir", lambda *args, **kwargs: None)
    monkeypatch.setattr(push, "unique_pod_name", lambda: "loader-pod")
    monkeypatch.setattr(push, "get_minikube_docker_env", lambda: {})
    monkeypatch.setattr(push, "cp", lambda src, dst: copy_calls.append((src, dst)))
    monkeypatch.setattr(
        push,
        "exec_cmd",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    result = push.push_models(
        app_name="test-app",
        horizons=["rps_t10m"],
        data_csv=str(csv_path),
    )

    assert result.success is True
    assert any(
        dst.endswith("/models/rps_t10m/ppa_model.tflite") for _, dst in copy_calls
    )
    assert any(
        dst.endswith("/models/rps_t10m/ppa_model_metadata.json")
        for _, dst in copy_calls
    )
    assert any(dst.endswith("/models/rps_t10m/scaler.pkl") for _, dst in copy_calls)
    assert any(
        dst.endswith("/models/rps_t10m/target_scaler.pkl") for _, dst in copy_calls
    )


def test_push_models_uses_pipeline_output_when_champion_missing(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    artifacts_dir = project_root / "model" / "artifacts"
    artifacts_dir.mkdir(parents=True)

    # Canonical filenames (no horizon suffix)
    (artifacts_dir / "ppa_model.tflite").write_bytes(b"TFL3")
    metadata_payload = {"feature_columns": ["a"], "lookback": 60}
    (artifacts_dir / "ppa_model_metadata.json").write_text(json.dumps(metadata_payload))

    csv_path = tmp_path / "training.csv"
    csv_path.write_text("timestamp,normalized_rps_t10m\n2026-04-09T00:00:00Z,1\n")

    copy_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(push, "PROJECT_DIR", project_root)
    monkeypatch.setattr(push, "MODEL_DIR", project_root / "models")
    monkeypatch.setattr(push, "CHAMPION_DIR", tmp_path / "champions")
    monkeypatch.setattr(push, "validate_cluster", lambda: True)
    monkeypatch.setattr(push, "ensure_exists", lambda *args, **kwargs: True)
    monkeypatch.setattr(push, "create_loader_pod", lambda *args, **kwargs: None)
    monkeypatch.setattr(push, "wait_for_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(push, "delete_pod", lambda *args, **kwargs: None)
    monkeypatch.setattr(push, "mkdir", lambda *args, **kwargs: None)
    monkeypatch.setattr(push, "unique_pod_name", lambda: "loader-pod")
    monkeypatch.setattr(push, "get_minikube_docker_env", lambda: {})
    monkeypatch.setattr(push, "cp", lambda src, dst: copy_calls.append((src, dst)))
    monkeypatch.setattr(
        push,
        "exec_cmd",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    result = push.push_models(
        app_name="test-app",
        horizons=["normalized_rps_t10m"],
        data_csv=str(csv_path),
    )

    assert result.success is True
    # Canonical filenames: no horizon suffix
    assert any("model/artifacts/ppa_model.tflite" in src for src, _ in copy_calls)
    assert any(
        "model/artifacts/ppa_model_metadata.json" in src for src, _ in copy_calls
    )
    assert (
        push.CHAMPION_DIR / "test-app" / "normalized_rps_t10m" / "ppa_model.tflite"
    ).exists()
    assert (
        push.CHAMPION_DIR
        / "test-app"
        / "normalized_rps_t10m"
        / "ppa_model_metadata.json"
    ).exists()
