import sys
import types
from pathlib import Path

from ppa.cli.commands import model as model_cmd


def test_model_convert_resolves_structured_path(monkeypatch, tmp_path):
    calls = {}

    def fake_convert_model(*, model_path, quantize, output_path):
        calls["model_path"] = model_path
        calls["quantize"] = quantize
        calls["output_path"] = output_path
        Path(model_path).parent.mkdir(parents=True, exist_ok=True)
        Path(model_path).write_text("model")
        return {"output_path": output_path, "size_kb": 1.0}

    fake_model_pkg = types.ModuleType("ppa.model")
    fake_model_pkg.__path__ = []
    fake_convert_mod = types.ModuleType("ppa.model.convert")
    fake_convert_mod.convert_model = fake_convert_model

    monkeypatch.setitem(sys.modules, "ppa.model", fake_model_pkg)
    monkeypatch.setitem(sys.modules, "ppa.model.convert", fake_convert_mod)

    model_cmd.model_convert(
        app_name="app",
        namespace="ns",
        target="rps_t10m",
        root_dir=str(tmp_path),
        output=None,
        no_quantize=False,
    )

    # Expect canonical paths (no horizon suffix in filenames)
    assert calls["model_path"] == str(tmp_path / "app" / "ns" / "rps_t10m" / "ppa_model.keras")
    assert calls["output_path"] == str(tmp_path / "app" / "ns" / "rps_t10m" / "ppa_model.tflite")
    assert calls["quantize"] is True
