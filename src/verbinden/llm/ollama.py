"""Ollama chat JSON adapter with injectable HTTP transport."""
from collections.abc import Callable
from time import sleep as default_sleep

import httpx

from .base import LlmError, generate_with_retry


class OllamaLlm:
    def __init__(self, *, model: str, base_url: str = 'http://localhost:11434',
                 max_retries: int = 3, timeout_seconds: int = 60,
                 transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = default_sleep) -> None:
        self._model = model
        self._url = base_url.rstrip('/') + '/api/chat'
        self._max_retries = max_retries
        self._sleep = sleep
        self._client = httpx.Client(timeout=timeout_seconds, transport=transport,
                                    follow_redirects=False)

    def generate_json(self, prompt: str) -> str:
        def request() -> str:
            response = self._client.post(self._url, json={
                'model': self._model, 'messages': [{'role': 'user', 'content': prompt}],
                'format': 'json', 'stream': False,
            })
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get('message'), dict):
                raise LlmError('Ollama の応答形式が不正です')
            return payload['message'].get('content')
        return generate_with_retry(request, self._max_retries, self._sleep)

    def close(self) -> None:
        self._client.close()
