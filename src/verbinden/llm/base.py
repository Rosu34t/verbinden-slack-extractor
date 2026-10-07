"""LLM contracts and bounded, privacy-preserving retries."""
from collections.abc import Callable
import json
from typing import Protocol, runtime_checkable

import httpx


class LlmError(RuntimeError):
    """A sanitized provider failure; provider payloads are never included."""


@runtime_checkable
class LlmClient(Protocol):
    def generate_json(self, prompt: str) -> str: ...


def validate_json(text: object) -> str:
    if not isinstance(text, str) or not text.strip():
        raise LlmError('LLM が空の応答を返しました')
    try:
        json.loads(text)
    except (ValueError, RecursionError):
        raise LlmError('LLM が有効な JSON を返しませんでした') from None
    return text


def retryable(error: Exception) -> bool:
    if isinstance(error, (httpx.TransportError, ConnectionError, TimeoutError)):
        return True
    status = getattr(error, 'code', None)
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
    return isinstance(status, int) and (status == 429 or 500 <= status < 600)


def generate_with_retry(operation: Callable[[], str], max_retries: int,
                        sleep: Callable[[float], None]) -> str:
    for attempt in range(max_retries + 1):
        try:
            return validate_json(operation())
        except Exception as error:
            if retryable(error) and attempt < max_retries:
                sleep(2 ** (attempt + 1))
                continue
            if isinstance(error, LlmError):
                raise
            raise LlmError('LLM のリクエストに失敗しました') from None
    raise AssertionError('unreachable')
