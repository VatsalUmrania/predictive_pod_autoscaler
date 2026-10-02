"""
Factory and discovery for LLM providers.
"""

from __future__ import annotations

import logging
import os

from nexus.reasoning.llm.anthropic import AnthropicProvider
from nexus.reasoning.llm.base import LLMProvider, NullProvider
from nexus.reasoning.llm.gemini import GeminiProvider
from nexus.reasoning.llm.openai import OpenAIProvider

logger = logging.getLogger(__name__)

_PROVIDERS = {
    "gemini": GeminiProvider,
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "none": NullProvider,
}

_cached_provider: LLMProvider | None = None


def _autodetect_provider() -> str:
    """Return provider name based on which API key is set in the environment."""
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.getenv("NEXUS_LLM_API_KEY"):
        return "gemini"
    return "none"


def get_llm_provider(
    provider: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> LLMProvider:
    """
    Return the configured LLM provider singleton.

    Resolution order:
        1. `provider` argument
        2. NEXUS_LLM_PROVIDER env var
        3. Auto-detect: use whichever key is present (Gemini → OpenAI → Anthropic)
        4. NullProvider (rule-based fallback only)
    """
    global _cached_provider

    provider_name = (
        provider or os.getenv("NEXUS_LLM_PROVIDER", "") or _autodetect_provider()
    ).lower()

    expected_model = model or os.getenv("NEXUS_LLM_MODEL")

    if _cached_provider is not None and provider is None and api_key is None and model is None:
        env_provider_match = (provider_name == "") or (_cached_provider.name == provider_name)
        env_model_match = (expected_model is None) or (_cached_provider.model == expected_model)
        if env_provider_match and env_model_match:
            return _cached_provider

    cls = _PROVIDERS.get(provider_name, NullProvider)
    if cls is NullProvider:
        instance: LLMProvider = NullProvider()
    else:
        instance = cls(api_key=api_key, model=model)

    if instance.is_available():
        logger.info(f"[LLM] Provider: {instance.name} | Model: {instance.model}")
    else:
        logger.info(
            f"[LLM] Provider '{instance.name}' has no API key — "
            "RCA will use rule-based fallback only"
        )

    _cached_provider = instance
    return instance
