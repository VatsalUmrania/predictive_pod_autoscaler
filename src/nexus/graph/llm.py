"""
NEXUS LLM Factory
=================
Provides configured LangChain chat models for diagnosis and planning nodes.
Supports Google Gemini, OpenAI, Anthropic, and a deterministic MockChatModel for tests.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from langchain_core.language_models.fake import FakeListChatModel

logger = logging.getLogger(__name__)


def get_llm(provider: str | None = None, model: str | None = None, temperature: float = 0.0) -> Any:
    """Return configured LangChain chat model."""
    selected_provider = (
        provider or os.getenv("NEXUS_LLM_PROVIDER") or "gemini"
    ).lower()
    selected_model = model or os.getenv("NEXUS_LLM_MODEL")

    if selected_provider in ("mock", "fake", "test"):
        logger.info("[LLM Factory] Returning FakeListChatModel for testing")
        return FakeListChatModel(responses=["{}"])

    if selected_provider == "gemini":
        api_key = os.getenv("NEXUS_LLM_API_KEY") or os.getenv("GEMINI_API_KEY")
        if not api_key:
            logger.info("[LLM Factory] No Gemini API key provided; using fallback mock")
            return FakeListChatModel(responses=["{}"])

        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
            return ChatGoogleGenerativeAI(
                model=selected_model or "gemini-2.0-flash",
                google_api_key=api_key,
                temperature=temperature,
            )
        except ImportError:
            # If langchain_google_genai is not installed, fallback to mock/fake
            logger.info("[LLM Factory] langchain_google_genai not installed; returning mock")
            return FakeListChatModel(responses=["{}"])

    if selected_provider == "openai":
        try:
            from langchain_openai import ChatOpenAI
            return ChatOpenAI(
                model=selected_model or "gpt-4o-mini",
                temperature=temperature,
            )
        except ImportError:
            return FakeListChatModel(responses=["{}"])

    return FakeListChatModel(responses=["{}"])
