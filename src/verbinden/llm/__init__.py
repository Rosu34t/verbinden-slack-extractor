"""Construct the configured provider without making a network request."""
from collections.abc import Callable
from time import sleep as default_sleep

from verbinden.config import RuntimeConfig
from .base import LlmClient, LlmError
from .fake import FakeLlm
from .gemini import GeminiLlm
from .ollama import OllamaLlm


def create_llm_client(config: RuntimeConfig, *,
                      sleep: Callable[[float], None] = default_sleep) -> LlmClient:
    settings = config.config.llm
    if settings.provider == 'fake':
        return FakeLlm()
    options = dict(model=settings.model, max_retries=settings.max_retries,
                   timeout_seconds=settings.timeout_seconds, sleep=sleep)
    if settings.provider == 'gemini':
        if config.gemini_api_key is None:
            raise LlmError('GEMINI_API_KEY が設定されていません')
        return GeminiLlm(api_key=config.gemini_api_key, **options)
    return OllamaLlm(base_url=config.ollama_base_url, **options)


__all__ = ['create_llm_client', 'LlmClient', 'LlmError', 'FakeLlm']
