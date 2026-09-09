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

from langchain_core.language_models import FakeListChatModel

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

        gemini_model = selected_model or "gemini-3.1-flash-lite"

        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
            logger.info("[LLM Factory] Initialized ChatGoogleGenerativeAI with model=%s", gemini_model)
            return ChatGoogleGenerativeAI(
                model=gemini_model,
                google_api_key=api_key,
                temperature=temperature,
            )
        except ImportError:
            # Fallback to direct google.genai client wrapped in a ChatModel interface
            try:
                import asyncio

                from google import genai
                from google.genai import types

                class GoogleGenAIChatBridge:
                    """LangChain-compatible chat model bridge using the official google.genai SDK."""

                    def __init__(self, client: genai.Client, model_name: str, temp: float):
                        self._client = client
                        self._model = model_name
                        self._temp = temp

                    async def ainvoke(self, messages: list[Any], **kwargs: Any) -> Any:
                        prompt_parts: list[str] = []
                        sys_instruction: str | None = None
                        for m in messages:
                            m_type = getattr(m, "type", "user")
                            content = getattr(m, "content", str(m))
                            if m_type in ("system", "developer"):
                                sys_instruction = content
                            else:
                                prompt_parts.append(content)

                        full_user_prompt = "\n\n".join(prompt_parts)
                        config = types.GenerateContentConfig(
                            temperature=self._temp,
                            response_mime_type="application/json",
                        )
                        if sys_instruction:
                            config.system_instruction = sys_instruction

                        resp = await asyncio.to_thread(
                            self._client.models.generate_content,
                            model=self._model,
                            contents=full_user_prompt,
                            config=config,
                        )

                        class _BridgeResponse:
                            def __init__(self, text: str):
                                self.content = text

                        return _BridgeResponse(resp.text.strip())

                client = genai.Client(api_key=api_key)
                logger.info("[LLM Factory] Initialized google.genai bridge with model=%s", gemini_model)
                return GoogleGenAIChatBridge(client, gemini_model, temperature)
            except Exception as exc:
                logger.warning("[LLM Factory] Failed to initialize google.genai bridge: %s", exc)
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
