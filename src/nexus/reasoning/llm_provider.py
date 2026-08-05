"""
NEXUS Multi-LLM Provider
=========================
Unified interface for RCA reasoning across multiple LLM backends.

Supported providers (set via NEXUS_LLM_PROVIDER):
    gemini      — Google Gemini (default)
    openai      — OpenAI GPT-4o / GPT-3.5
    anthropic   — Anthropic Claude
    none        — Disable LLM; always use rule-based fallback

Environment variables:
    NEXUS_LLM_PROVIDER      — Provider name (gemini | openai | anthropic | none)
    NEXUS_LLM_API_KEY       — API key for the selected provider
    NEXUS_LLM_MODEL         — Model name override (optional)
    NEXUS_RCA_TIMEOUT_S     — Request timeout in seconds (default: 10)

Architecture:
    LLMProvider is an abstract base.
    Concrete subclasses: GeminiProvider, OpenAIProvider, AnthropicProvider.
    RCAEngine imports get_llm_provider() and calls provider.complete(prompt).
    The provider returns raw text; RCAEngine handles JSON parsing.
"""

from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

# System instruction (shared across all providers)
SYSTEM_INSTRUCTION = """\
You are NEXUS, an autonomous cloud infrastructure Root Cause Analysis (RCA) system.
You receive correlated incident signals from multiple domain agents monitoring a production application.
You support both Kubernetes workloads and AWS serverless applications (Lambda, API Gateway, SQS, DynamoDB).

Your task: analyze the signals and determine the most likely root cause.

Rules:
- Be specific and technical — not generic filler text
- Prefer the simplest hypothesis that explains all signals (Occam's razor)
- Use the available runbook list to constrain your action recommendation
- healing_level 0 = alert only, 1 = no-regret (restart), 2 = bounded mitigation (scale/memory increase/canary halt), 3 = significant change (rollout undo/Lambda alias rollback)
- confidence 0.0-1.0 — be conservative; prefer 0.5-0.8 range unless signals are deterministic
- If multiple explanations are equally plausible, choose the more conservative (lower healing_level)

Available Kubernetes runbooks:
- runbook_pod_crashloop_v1 (L1): Restart pod + VPA hint
- runbook_high_error_rate_post_deploy_v1 (L2): Halt canary + alert
- runbook_missing_env_key_v1 (L0): Block deploy + alert
- runbook_dns_resolution_failure_v1 (L1): Flush CoreDNS cache + escalate
- runbook_db_connection_exhaustion_v1 (L2): Alert + annotate deployment

Available AWS Serverless runbooks:
- runbook_lambda_error_spike_v1 (L3): Alert + rollback Lambda alias to previous version
- runbook_lambda_throttle_v1 (L2): Alert + increase Lambda reserved concurrency
- runbook_lambda_timeout_v1 (L2): Alert + increase Lambda timeout
- runbook_lambda_oom_v1 (L2): Alert + increase Lambda memory allocation
- runbook_sqs_dlq_v1 (L2): Alert + replay DLQ messages to source queue
- runbook_dynamo_throttle_v1 (L0): Alert only — capacity change requires human review

Respond ONLY with valid JSON. No markdown fences, no prose outside the JSON structure.

Required schema:
{
  "root_cause": "string — 1-2 sentences, specific technical cause",
  "failure_class": "one of: bad_deploy | resource_exhaustion | dependency_failure | config_error | cascading_failure | unknown",
  "healing_level": 0,
  "runbook_id": "exact runbook ID from the list above, or null",
  "confidence": 0.0,
  "reasoning": "string — 2-3 sentences of chain-of-thought",
  "actions_to_avoid": ["list of action types that would make this worse"]
}\
"""

# Abstract base
class LLMProvider(ABC):
    """Abstract base for all LLM provider backends."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable provider name."""

    @property
    @abstractmethod
    def model(self) -> str:
        """Model name being used."""

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if the provider is configured and ready to use."""

    @abstractmethod
    async def complete(self, user_prompt: str) -> str:
        """
        Send a prompt and return the raw text response.
        Raises on error — caller handles fallback.
        """

# Gemini provider
class GeminiProvider(LLMProvider):
    """Google Gemini via google-genai SDK."""

    DEFAULT_MODEL = "gemini-3.1-flash-lite"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self._api_key = api_key or os.getenv("NEXUS_LLM_API_KEY", "")
        self._model_name = self.DEFAULT_MODEL
        self._client = None

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
        try:
            from google import genai

            self._client = genai.Client(api_key=self._api_key)
        except ImportError:
            logger.warning("[LLM] google-genai not installed: pip install google-genai")
            return False
        except Exception as exc:
            logger.warning(f"[LLM] Gemini init failed: {exc}")
            return False

        logger.info("[LLM] Gemini client ready — model=%s", self._model_name)
        return True

    def _sync_complete(self, prompt: str) -> str:
        if not self._ensure_client():
            raise RuntimeError("Gemini client not available")
        from google.genai import types

        response = self._client.models.generate_content(
            model=self._model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                temperature=0.2,
                max_output_tokens=512,
                response_mime_type="application/json",
            ),
        )
        return response.text.strip()

    async def complete(self, user_prompt: str) -> str:
        return await asyncio.to_thread(self._sync_complete, user_prompt)

# OpenAI provider
class OpenAIProvider(LLMProvider):
    """OpenAI ChatCompletion via openai SDK."""

    DEFAULT_MODEL = "gpt-4o-mini"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self._api_key = (
            api_key or os.getenv("NEXUS_LLM_API_KEY") or os.getenv("OPENAI_API_KEY", "")
        )
        self._model_name = model or os.getenv("NEXUS_LLM_MODEL", self.DEFAULT_MODEL)
        self._client = None

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
        return response.choices[0].message.content.strip()

    async def complete(self, user_prompt: str) -> str:
        return await asyncio.to_thread(self._sync_complete, user_prompt)

# Anthropic provider
class AnthropicProvider(LLMProvider):
    """Anthropic Claude via anthropic SDK."""

    DEFAULT_MODEL = "claude-3-haiku-20240307"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self._api_key = (
            api_key
            or os.getenv("NEXUS_LLM_API_KEY")
            or os.getenv("ANTHROPIC_API_KEY", "")
        )
        self._model_name = model or os.getenv("NEXUS_LLM_MODEL", self.DEFAULT_MODEL)
        self._client = None

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
        return text_block.text.strip()

    async def complete(self, user_prompt: str) -> str:
        return await asyncio.to_thread(self._sync_complete, user_prompt)

# No-op provider (rule-based only mode)
class NullProvider(LLMProvider):
    """Placeholder when LLM is explicitly disabled (NEXUS_LLM_PROVIDER=none)."""

    @property
    def name(self) -> str:
        return "none"

    @property
    def model(self) -> str:
        return "none"

    def is_available(self) -> bool:
        return False

    async def complete(self, user_prompt: str) -> str:
        raise RuntimeError("LLM disabled (NEXUS_LLM_PROVIDER=none)")

# Factory
_PROVIDERS = {
    "gemini": GeminiProvider,
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "none": NullProvider,
}

_cached_provider: LLMProvider | None = None

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
    if _cached_provider is not None and provider is None and api_key is None:
        return _cached_provider

    provider_name = (
        provider or os.getenv("NEXUS_LLM_PROVIDER", "") or _autodetect_provider()
    ).lower()

    cls = _PROVIDERS.get(provider_name, NullProvider)
    instance: LLMProvider

    if cls is NullProvider:
        instance = NullProvider()
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

def _autodetect_provider() -> str:
    """Return provider name based on which API key is set in the environment.

    Provider-specific keys win over the generic NEXUS_LLM_API_KEY. This way,
    setting both ``NEXUS_LLM_API_KEY`` and ``OPENAI_API_KEY`` will pick OpenAI
    rather than silently defaulting to Gemini.
    """
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.getenv("NEXUS_LLM_API_KEY"):
        return "gemini"
    return "none"
