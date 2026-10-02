"""
Google Gemini provider implementation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any

from nexus.reasoning.llm.base import SYSTEM_INSTRUCTION, LLMProvider

logger = logging.getLogger(__name__)


class GeminiProvider(LLMProvider):
    """Google Gemini via google-genai SDK."""

    DEFAULT_MODEL = "gemini-3.1-flash-lite"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self._api_key = api_key or os.getenv("NEXUS_LLM_API_KEY", "")
        self._model_name: str = model or os.getenv("NEXUS_LLM_MODEL") or self.DEFAULT_MODEL
        self._client: Any = None
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "gemini"

    @property
    def model(self) -> str:
        return self._model_name

    def is_available(self) -> bool:
        return bool(self._api_key)

    def _ensure_client(self) -> bool:
        if self._client is not None:
            return True
        if not self._api_key:
            return False
        with self._lock:
            if self._client is not None:
                return True
            try:
                from google import genai

                self._client = genai.Client(api_key=self._api_key)
                logger.info("[LLM] Gemini client ready — model=%s", self._model_name)
                return True
            except ImportError:
                logger.warning("[LLM] google-genai not installed: pip install google-genai")
                return False
            except Exception as exc:
                logger.warning(f"[LLM] Gemini init failed: {exc}")
                return False

    def _sync_complete(self, prompt: str) -> str:
        if not self._ensure_client():
            raise RuntimeError("Gemini client not available")
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            temperature=0.2,
            max_output_tokens=512,
            response_mime_type="application/json",
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        response = self._client.models.generate_content(
            model=self._model_name,
            contents=prompt,
            config=config,
        )
        return str(response.text).strip()

    async def complete(self, user_prompt: str) -> str:
        return await asyncio.to_thread(self._sync_complete, user_prompt)
