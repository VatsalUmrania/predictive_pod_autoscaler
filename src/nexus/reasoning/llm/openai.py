"""
OpenAI provider implementation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any

from nexus.reasoning.llm.base import SYSTEM_INSTRUCTION, LLMProvider

logger = logging.getLogger(__name__)


class OpenAIProvider(LLMProvider):
    """OpenAI ChatCompletion via openai SDK."""

    DEFAULT_MODEL = "gpt-4o-mini"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self._api_key = (
            api_key or os.getenv("NEXUS_LLM_API_KEY") or os.getenv("OPENAI_API_KEY", "")
        )
        self._model_name: str = (
            model or os.getenv("NEXUS_LLM_MODEL") or self.DEFAULT_MODEL
        )
        self._client: Any = None
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "openai"

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
                from openai import OpenAI

                self._client = OpenAI(api_key=self._api_key)
                logger.info(f"[LLM] OpenAI client ready — model={self._model_name}")
                return True
            except ImportError:
                logger.warning("[LLM] openai not installed: pip install openai")
            except Exception as exc:
                logger.warning(f"[LLM] OpenAI init failed: {exc}")
            return False

    def _sync_complete(self, prompt: str) -> str:
        if not self._ensure_client():
            raise RuntimeError("OpenAI client not available")
        response = self._client.chat.completions.create(
            model=self._model_name,
            messages=[
                {"role": "system", "content": SYSTEM_INSTRUCTION},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=512,
            response_format={"type": "json_object"},
        )
        return str(response.choices[0].message.content).strip()

    async def complete(self, user_prompt: str) -> str:
        return await asyncio.to_thread(self._sync_complete, user_prompt)
