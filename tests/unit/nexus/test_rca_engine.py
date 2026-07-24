"""Unit tests for nexus.reasoning.rca_engine — F2 enrichment + OpenAI fallback.

Covers the two behaviors added in the self-healing plan:
  - F2 LLM enrichment: a historical runbook from the KnowledgeBase is injected
    into the prompt context (``_build_gemini_prompt`` appends a Historical Note).
  - OpenAI fallback: when the Gemini primary throws/times out AND an OpenAI key
    is present, the engine retries via OpenAIProvider and tags source="openai".
  - Rule-based fallback still wins when both LLM paths fail.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.reasoning.rca_engine import RCAEngine, _build_gemini_prompt

# ── Helpers ───────────────────────────────────────────────────────────────────


def _entity_with_signals(signals: set[str]) -> MagicMock:
    """A lightweight stand-in for an IncidentCluster.

    ``analyze()`` only reads ``cluster.signal_types`` (for the KB lookup) and
    ``cluster.to_llm_context()`` (prompt building). signal_types is a read-only
    @property on the real class, so we use a plain MagicMock and shadow it with
    a real set — this lets us drive arbitrary signal strings without an enum
    member for each one.
    """
    cluster = MagicMock()
    cluster.signal_types = signals
    cluster.to_llm_context = MagicMock(return_value="BASE LLM CONTEXT")
    return cluster


def _engine_with_provider(provider: MagicMock) -> RCAEngine:
    """Build an RCAEngine whose resolved provider is replaced with ``provider``.

    Constructor calls get_llm_provider; we let that run (NullProvider) then
    overwrite ``self._provider`` directly so tests stay provider-agnostic.
    """
    engine = RCAEngine(api_key=None, timeout_s=2.0)
    engine._provider = provider
    return engine


# ── F2 enrichment: prompt building ────────────────────────────────────────────


def test_build_gemini_prompt_appends_historical_runbook_note():
    """When a historical runbook is supplied, the prompt gains a Historical Note."""
    cluster = _entity_with_signals({"pod_crashloop"})
    prompt = _build_gemini_prompt(cluster, historical_runbook="runbook_x_v1")
    assert "BASE LLM CONTEXT" in prompt
    assert "runbook_x_v1" in prompt
    assert "Historical Note" in prompt
    assert "Strongly consider it" in prompt


def test_build_gemini_prompt_without_historical_runbook_is_plain():
    """No runbook → prompt is just the base context (no Historical Note section)."""
    cluster = _entity_with_signals({"pod_crashloop"})
    prompt = _build_gemini_prompt(cluster, historical_runbook=None)
    assert prompt == "BASE LLM CONTEXT"
    assert "Historical Note" not in prompt


def test_build_gemini_prompt_empty_string_runbook_treated_as_absent():
    """An empty-string runbook (KB returned '') must not emit a note section."""
    cluster = _entity_with_signals({"pod_crashloop"})
    prompt = _build_gemini_prompt(cluster, historical_runbook="")
    assert "Historical Note" not in prompt


# ── F2 enrichment: KB lookup is driven into analyze() ─────────────────────────


@pytest.mark.asyncio
async def test_analyze_queries_knowledge_base_for_runbook(monkeypatch):
    """analyze() must call get_best_runbook_for_pattern when a KB is present."""
    cluster = _entity_with_signals({"pod_oomkilled"})

    captured_prompts: list[str] = []
    provider = MagicMock()
    provider.name = "gemini"
    provider.model = "gemini-pro"
    provider.is_available = MagicMock(return_value=True)

    async def fake_complete(prompt: str) -> str:
        captured_prompts.append(prompt)
        # Make _parse_response return None so we fall through to rule-based —
        # we only care that the KB was consulted and the prompt was enriched.
        return "not json"

    provider.complete = fake_complete

    kb = MagicMock()
    kb.get_best_runbook_for_pattern = AsyncMock(return_value="runbook_pod_crashloop_v1")

    engine = _engine_with_provider(provider)
    engine._knowledge_base = kb
    # NullProvider's name differs; force gemini so the OpenAI fallback branch is
    # evaluated (and skipped, NEXUS_OPENAI_API_KEY unset).
    engine._provider.name = "gemini"

    monkeypatch.delenv("NEXUS_OPENAI_API_KEY", raising=False)

    result = await engine.analyze(cluster)

    kb.get_best_runbook_for_pattern.assert_awaited_once()
    # The signal set passed to the KB lookup matches the cluster's.
    args, _ = kb.get_best_runbook_for_pattern.await_args
    assert args[0] == cluster.signal_types
    # And the prompt sent to the provider carried the historical note.
    assert any(
        "runbook_pod_crashloop_v1" in p and "Historical Note" in p
        for p in captured_prompts
    )
    # _parse_response fails on garbage → rule-based fallback still returns a result.
    assert result.source == "rule_based"


@pytest.mark.asyncio
async def test_analyze_without_knowledge_base_does_not_lookup(monkeypatch):
    """No KB → analyze skips the lookup entirely (graceful)."""
    monkeypatch.delenv("NEXUS_OPENAI_API_KEY", raising=False)
    cluster = _entity_with_signals({"pod_oomkilled"})

    provider = MagicMock()
    provider.name = "gemini"
    provider.is_available = MagicMock(return_value=True)
    provider.complete = AsyncMock(return_value="not json")

    engine = _engine_with_provider(provider)
    engine._knowledge_base = None  # explicitly absent

    result = await engine.analyze(cluster)
    assert result.source == "rule_based"


# ── OpenAI fallback ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_fallback_used_when_gemini_fails(monkeypatch):
    """Gemini raises → OpenAI available → result tagged source='openai'."""
    monkeypatch.setenv("NEXUS_OPENAI_API_KEY", "sk-test-openai")
    cluster = _entity_with_signals({"pod_oomkilled"})

    # Primary provider (gemini) is available but throws.
    gemini = MagicMock()
    gemini.name = "gemini"
    gemini.model = "gemini-pro"
    gemini.is_available = MagicMock(return_value=True)
    gemini.complete = AsyncMock(side_effect=RuntimeError("gemini 503"))

    # OpenAI provider returned by the patched import in the engine fallback.
    openai_instance = MagicMock()
    openai_instance.complete = AsyncMock(
        return_value=(
            '{"root_cause":"oom","failure_class":"resource_exhaustion",'
            '"healing_level":1,"runbook_id":"runbook_pod_crashloop_v1",'
            '"confidence":0.7,"reasoning":"openai said so"}'
        )
    )

    with patch(
        "nexus.reasoning.llm_provider.OpenAIProvider", return_value=openai_instance
    ):
        engine = _engine_with_provider(gemini)
        engine._knowledge_base = None
        result = await engine.analyze(cluster)

    assert result.source == "openai"
    assert result.failure_class == "resource_exhaustion"
    assert result.healing_level == 1
    assert result.runbook_id == "runbook_pod_crashloop_v1"
    # OpenAI was genuinely called with the (possibly enriched) prompt.
    openai_instance.complete.assert_awaited_once()
    gemini.complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_openai_fallback_skipped_when_no_openai_key(monkeypatch):
    """Gemini fails but no OpenAI key → straight to rule-based (no OpenAI call)."""
    monkeypatch.delenv("NEXUS_OPENAI_API_KEY", raising=False)
    cluster = _entity_with_signals({"pod_oomkilled"})

    gemini = MagicMock()
    gemini.name = "gemini"
    gemini.is_available = MagicMock(return_value=True)
    gemini.complete = AsyncMock(side_effect=asyncio.TimeoutError())

    with patch("nexus.reasoning.llm_provider.OpenAIProvider") as openai_cls:
        engine = _engine_with_provider(gemini)
        engine._knowledge_base = None
        result = await engine.analyze(cluster)

    openai_cls.assert_not_called()
    assert result.source == "rule_based"


@pytest.mark.asyncio
async def test_openai_fallback_skipped_when_primary_not_gemini(monkeypatch):
    """A non-gemini primary (anthropic) that fails does NOT try OpenAI fallback."""
    monkeypatch.setenv("NEXUS_OPENAI_API_KEY", "sk-test-openai")
    cluster = _entity_with_signals({"pod_oomkilled"})

    anthropic = MagicMock()
    anthropic.name = "anthropic"
    anthropic.is_available = MagicMock(return_value=True)
    anthropic.complete = AsyncMock(side_effect=RuntimeError("anthropic down"))

    with patch("nexus.reasoning.llm_provider.OpenAIProvider") as openai_cls:
        engine = _engine_with_provider(anthropic)
        engine._knowledge_base = None
        result = await engine.analyze(cluster)

    openai_cls.assert_not_called()
    assert result.source == "rule_based"


@pytest.mark.asyncio
async def test_openai_fallback_failure_falls_through_to_rules(monkeypatch):
    """Gemini AND OpenAI both fail → rule-based engine still yields a result."""
    monkeypatch.setenv("NEXUS_OPENAI_API_KEY", "sk-test-openai")
    cluster = _entity_with_signals({"pod_oomkilled"})

    gemini = MagicMock()
    gemini.name = "gemini"
    gemini.is_available = MagicMock(return_value=True)
    gemini.complete = AsyncMock(side_effect=RuntimeError("gemini 503"))

    openai_instance = MagicMock()
    openai_instance.complete = AsyncMock(side_effect=RuntimeError("openai 503"))

    with patch(
        "nexus.reasoning.llm_provider.OpenAIProvider", return_value=openai_instance
    ):
        engine = _engine_with_provider(gemini)
        engine._knowledge_base = None
        result = await engine.analyze(cluster)

    assert result.source == "rule_based"


# ── No provider available at all ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_primary_unavailable_uses_rule_based_immediately(monkeypatch):
    """Provider not available → skip LLM entirely, go to rule-based RCA."""
    monkeypatch.delenv("NEXUS_OPENAI_API_KEY", raising=False)
    cluster = _entity_with_signals({"pod_oomkilled"})

    provider = MagicMock()
    provider.name = "gemini"
    provider.is_available = MagicMock(return_value=False)
    provider.complete = AsyncMock(side_effect=AssertionError("must not be called"))

    engine = _engine_with_provider(provider)
    engine._knowledge_base = None
    result = await engine.analyze(cluster)

    provider.complete.assert_not_awaited()
    assert result.source == "rule_based"
    assert result.runbook_id == "runbook_pod_crashloop_v1"


@pytest.mark.asyncio
async def test_analyze_never_raises_on_total_failure(monkeypatch):
    """Contract: analyze() always returns a valid RCAResult, never raises."""
    monkeypatch.setenv("NEXUS_OPENAI_API_KEY", "sk-test-openai")
    cluster = _entity_with_signals({"unknown_signal_xyz"})

    gemini = MagicMock()
    gemini.name = "gemini"
    gemini.is_available = MagicMock(return_value=True)
    gemini.complete = AsyncMock(side_effect=Exception("total meltdown"))

    openai_instance = MagicMock()
    openai_instance.complete = AsyncMock(side_effect=Exception("also meltdown"))

    with patch(
        "nexus.reasoning.llm_provider.OpenAIProvider", return_value=openai_instance
    ):
        engine = _engine_with_provider(gemini)
        engine._knowledge_base = None
        result = await engine.analyze(cluster)

    assert result is not None
    assert result.source == "rule_based"
