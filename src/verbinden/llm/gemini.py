"""Gemini JSON adapter with injectable SDK and application-owned retries."""
from collections.abc import Callable
from time import sleep as default_sleep
from typing import Any

from google import genai
from google.genai import types
from pydantic import SecretStr

from .base import LlmError, generate_with_retry


class GeminiLlm:
    def __init__(self, *, api_key: SecretStr, model: str,
                 max_retries: int = 3, timeout_seconds: int = 60,
                 sdk_client: Any = None,
                 sleep: Callable[[float], None] = default_sleep) -> None:
        self._model = model
        self._max_retries = max_retries
        self._sleep = sleep
        self._options = types.HttpOptions(
            timeout=timeout_seconds * 1000,
            retry_options=types.HttpRetryOptions(attempts=1),
        )
        self._owned_client = sdk_client is None
        try:
            self._client = sdk_client if sdk_client is not None else genai.Client(
                api_key=api_key.get_secret_value(), http_options=self._options)
        except Exception:
            raise LlmError('Gemini クライアントを初期化できませんでした') from None

    def generate_json(self, prompt: str) -> str:
        def request() -> str:
            response = self._client.models.generate_content(
                model=self._model, contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type='application/json', http_options=self._options),
            )
            return response.text
        return generate_with_retry(request, self._max_retries, self._sleep)

    def close(self) -> None:
        if self._owned_client:
            self._client.close()
