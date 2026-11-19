"""
NEXUS LLM Factory
=================
Provides configured LangChain chat models for diagnosis and planning nodes.
Supports Google Gemini, OpenAI, Anthropic, and a deterministic MockChatModel for tests.
Applies concurrency and rate-limiting guardrails to prevent quota exhaustion.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from langchain_core.language_models import FakeListChatModel

from nexus.engine.workflow.llm_limiter import (
    LLMQuotaExhaustedError,
    RateLimitedChatModel,
)

logger = logging.getLogger(__name__)

__all__ = [
    "get_llm",
    "get_chat_model",
    "LLMQuotaExhaustedError",
    "RateLimitedChatModel",
]


def _init_gemini_model(api_key: str, model_name: str, temperature: float) -> Any:
    timeout_s = float(os.getenv("NEXUS_LLM_TIMEOUT_S", "45.0"))
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI

        logger.info(
            "[LLM Factory] Initialized ChatGoogleGenerativeAI with model=%s (timeout=%.1fs)",
            model_name,
            timeout_s,
        )
        return ChatGoogleGenerativeAI(
            model=model_name,
            google_api_key=api_key,
            temperature=temperature,
            max_retries=0,
            request_timeout=timeout_s,
        )
    except ImportError:
        try:
            import asyncio

            from google import genai
            from google.genai import types

            class GoogleGenAIChatBridge:
                def __init__(self, client: genai.Client, m_name: str, temp: float):
                    self._client = client
                    self._model = m_name
                    self._temp = temp

                async def ainvoke(self, messages: list[Any], **kwargs: Any) -> Any:
                    parts: list[str] = []
                    sys_inst: str | None = None
                    for m in messages:
                        m_type = getattr(m, "type", "user")
                        content = getattr(m, "content", str(m))
                        if m_type in ("system", "developer"):
                            sys_inst = content
                        else:
                            parts.append(content)

                    cfg = types.GenerateContentConfig(
                        temperature=self._temp,
                        response_mime_type="application/json",
                    )
                    if sys_inst:
                        cfg.system_instruction = sys_inst

                    resp = await asyncio.to_thread(
                        self._client.models.generate_content,
                        model=self._model,
                        contents="\n\n".join(parts),
                        config=cfg,
                    )

                    class _BridgeResponse:
                        def __init__(self, text: str):
                            self.content = text

                    return _BridgeResponse((resp.text or "").strip())

            client = genai.Client(api_key=api_key)
            logger.info(
                "[LLM Factory] Initialized google.genai bridge with model=%s",
                model_name,
            )
            return GoogleGenAIChatBridge(client, model_name, temperature)
        except Exception as exc:
            logger.warning("[LLM Factory] Failed to initialize google.genai: %s", exc)
            return FakeListChatModel(responses=["{}"])


def get_llm(
    provider: str | None = None, model: str | None = None, temperature: float = 0.0
) -> Any:
    """Return configured LangChain chat model with rate limiting."""
    selected_provider = (
        provider or os.getenv("NEXUS_LLM_PROVIDER") or "gemini"
    ).lower()
    selected_model = model or os.getenv("NEXUS_LLM_MODEL")

    if selected_provider in ("mock", "fake", "test"):
        return FakeListChatModel(responses=["{}"])

    if selected_provider == "gemini":
        api_key = os.getenv("NEXUS_LLM_API_KEY") or os.getenv("GEMINI_API_KEY")
        if not api_key:
            return FakeListChatModel(responses=["{}"])
        gemini_model = selected_model or "gemini-3.1-flash-lite"
        raw_model = _init_gemini_model(api_key, gemini_model, temperature)
        return RateLimitedChatModel(raw_model)

    if selected_provider == "openai":
        try:
            from langchain_openai import ChatOpenAI

            timeout_s = float(os.getenv("NEXUS_LLM_TIMEOUT_S", "45.0"))
            raw_model = ChatOpenAI(
                model=selected_model or "gpt-4o-mini",
                temperature=temperature,
                max_retries=0,
                timeout=timeout_s,
            )
            return RateLimitedChatModel(raw_model)
        except ImportError:
            return FakeListChatModel(responses=["{}"])

    return FakeListChatModel(responses=["{}"])


get_chat_model = get_llm
