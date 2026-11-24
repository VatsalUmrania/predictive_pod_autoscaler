import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def test_config():
    from ppa.config import Config

    return Config.from_env()


@pytest.fixture(autouse=True)
def _mock_llm_provider(monkeypatch):
    """Force the mock LLM provider so tests never hit a real API (e.g. when a
    developer has GEMINI_API_KEY exported locally). Per-test monkeypatch.setenv
    still overrides this."""
    monkeypatch.setenv("NEXUS_LLM_PROVIDER", "mock")
