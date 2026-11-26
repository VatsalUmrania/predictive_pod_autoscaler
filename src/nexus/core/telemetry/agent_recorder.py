"""
Agent Run Recorder
===================
Records what the NEXUS workflow's reasoning nodes actually did: one ``agent_runs``
row per graph node, plus one ``agent_messages`` row per LLM call and per tool
observation.

The recorder is deliberately forgiving. Postgres is an optional dependency at
runtime and observability must never be able to break incident handling, so
every database call is wrapped and degrades to a no-op.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "AgentRecorder",
    "extract_usage",
    "resolve_model_name",
    "split_content",
]


def resolve_model_name(llm: Any) -> str:
    """Best-effort model name for a chat model instance."""
    for attribute in ("model_name", "model", "model_id", "deployment_name"):
        value = getattr(llm, attribute, None)
        if isinstance(value, str) and value:
            return value
    return "unknown"


def extract_usage(message: Any) -> tuple[int, int]:
    """Return ``(prompt_tokens, completion_tokens)`` for a LangChain message.

    ``usage_metadata`` is the modern location; Gemini's native REST payload
    lands in ``response_metadata["token_usage"]`` under different key names.
    """
    prompt = 0
    completion = 0

    usage = getattr(message, "usage_metadata", None)
    if isinstance(usage, dict):
        prompt = int(usage.get("input_tokens") or 0)
        completion = int(usage.get("output_tokens") or 0)

    if not prompt and not completion:
        metadata = getattr(message, "response_metadata", None)
        if isinstance(metadata, dict):
            token_usage = metadata.get("token_usage")
            if isinstance(token_usage, dict):
                prompt = int(
                    token_usage.get("prompt_token_count")
                    or token_usage.get("input_tokens")
                    or 0
                )
                completion = int(
                    token_usage.get("candidates_token_count")
                    or token_usage.get("output_tokens")
                    or 0
                )

    return prompt, completion


def split_content(message: Any) -> tuple[str | None, str | None]:
    """Split a LangChain message into ``(text, reasoning)``.

    Some providers return the answer as a list of content blocks where the
    thinking blocks are interleaved with the answer blocks. When that happens
    the two are separated here so the dashboard can show the reasoning trace
    next to the answer.
    """
    content = getattr(message, "content", None)

    if isinstance(content, str):
        text: str | None = content
        reasoning: str | None = None
    elif isinstance(content, list):
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                text_parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "")
            if block_type in ("thinking", "reasoning"):
                thinking = block.get("thinking") or block.get("reasoning") or ""
                if thinking:
                    reasoning_parts.append(str(thinking))
            else:
                value = block.get("text")
                if value:
                    text_parts.append(str(value))
        text = "\n".join(text_parts) if text_parts else None
        reasoning = "\n".join(reasoning_parts) if reasoning_parts else None
    else:
        text = None if content is None else str(content)
        reasoning = None

    additional = getattr(message, "additional_kwargs", None)
    if isinstance(additional, dict):
        extra = additional.get("reasoning_content") or additional.get("thought")
        if extra and not reasoning:
            reasoning = str(extra)

    return text, reasoning


class AgentRecorder:
    """Records one graph node's LLM activity into ``agent_runs``/``agent_messages``."""

    def __init__(
        self,
        db_client: Any,
        incident_id: str,
        node_name: str,
        model_name: str = "unknown",
    ) -> None:
        self._db = db_client
        self.incident_id = incident_id
        self.node_name = node_name
        self.model_name = model_name
        self.run_id: str | None = None
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self._sequence = 0
        self._started_at: float | None = None

    async def start(self) -> str | None:
        """Open the run. Returns the run id, or None when the DB is unavailable."""
        if self._db is None:
            return None
        try:
            self.run_id = await self._db.start_agent_run(
                self.incident_id, self.node_name, self.model_name
            )
            self._started_at = time.monotonic()
            return self.run_id
        except Exception as exc:
            logger.debug(
                "[AgentRecorder] run start skipped for %s/%s: %s",
                self.incident_id,
                self.node_name,
                exc,
            )
            self.run_id = None
            return None

    async def log(
        self,
        role: str,
        content: str | None,
        reasoning_content: str | None = None,
    ) -> None:
        """Append one message to the run."""
        if self.run_id is None:
            return
        self._sequence += 1
        try:
            await self._db.log_message(
                self.run_id,
                self._sequence,
                role,
                content or "",
                reasoning_content=reasoning_content,
            )
        except Exception as exc:
            logger.debug(
                "[AgentRecorder] message log skipped for %s/%s: %s",
                self.incident_id,
                self.node_name,
                exc,
            )

    async def log_assistant(self, message: Any) -> None:
        """Record an LLM response, including its token usage and reasoning."""
        text, reasoning = split_content(message)
        prompt, completion = extract_usage(message)
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        await self.log("assistant", text, reasoning)

    async def finish(
        self,
        status: str = "completed",
        error_message: str | None = None,
    ) -> None:
        """Close the run with its final token counts and duration."""
        if self.run_id is None:
            return
        duration_ms = None
        if self._started_at is not None:
            duration_ms = int((time.monotonic() - self._started_at) * 1000)
        try:
            await self._db.finish_agent_run(
                self.run_id,
                status=status,
                prompt_tokens=self.prompt_tokens,
                completion_tokens=self.completion_tokens,
                duration_ms=duration_ms,
                error_message=error_message,
            )
        except Exception as exc:
            logger.debug(
                "[AgentRecorder] run finish skipped for %s/%s: %s",
                self.incident_id,
                self.node_name,
                exc,
            )
        finally:
            self.run_id = None
