"""
Anthropic Claude provider implementation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any

from nexus.reasoning.llm.base import SYSTEM_INSTRUCTION, LLMProvider

logger = logging.getLogger(__name__)


class AnthropicProvider(LLMProvider):
    """Anthropic Claude via anthropic SDK."""

    DEFAULT_MODEL = "claude-3-haiku-20240307"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self._api_key = (
            api_key
            or os.getenv("NEXUS_LLM_API_KEY")
            or os.getenv("ANTHROPIC_API_KEY", "")
        )
        self._model_name: str = model or os.getenv("NEXUS_LLM_MODEL") or self.DEFAULT_MODEL
        self._client: Any = None
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "anthropic"

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
                import anthropic

                self._client = anthropic.Anthropic(api_key=self._api_key)
                logger.info(f"[LLM] Anthropic client ready — model={self._model_name}")
                return True
            except ImportError:
                logger.warning("[LLM] anthropic not installed: pip install anthropic")
            except Exception as exc:
                logger.warning(f"[LLM] Anthropic init failed: {exc}")
            return False

    def _sync_complete(self, prompt: str) -> str:
        if not self._ensure_client():
            raise RuntimeError("Anthropic client not available")
        message = self._client.messages.create(
            model=self._model_name,
            max_tokens=512,
            system=SYSTEM_INSTRUCTION,
            messages=[{"role": "user", "content": prompt}],
        )
        text_block = next((blk for blk in message.content if blk.type == "text"), None)
        if text_block is None:
            raise RuntimeError("No text block found in Claude response")
        return str(text_block.text).strip()

    async def complete(self, user_prompt: str) -> str:
        return await asyncio.to_thread(self._sync_complete, user_prompt)
