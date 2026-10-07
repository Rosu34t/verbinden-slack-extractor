"""Provider contracts exercised with in-process transports only."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

from google.genai import errors
import httpx
from pydantic import SecretStr
import pytest

from verbinden.llm import create_llm_client
from verbinden.llm.base import LlmError
from verbinden.llm.gemini import GeminiLlm
from verbinden.llm.ollama import OllamaLlm
from test_llm import runtime


@pytest.mark.parametrize('failure', [
    errors.ClientError(429, {'error': {'message': 'secret'}}),
    errors.ServerError(503, {'error': {'message': 'secret'}}),
    httpx.ConnectError('secret'), httpx.ReadTimeout('secret'),
])
def test_gemini_retries_and_then_succeeds(failure):
    sdk = Mock()
    sdk.models.generate_content.side_effect = [failure, SimpleNamespace(text='{}')]
    sleeps = []
    client = GeminiLlm(api_key=SecretStr('secret'), model='test', sdk_client=sdk,
                       sleep=sleeps.append)
    assert client.generate_json('private') == '{}'
    assert sdk.models.generate_content.call_count == 2
    assert sleeps == [2]


def test_gemini_auth_error_is_not_retried_or_exposed(caplog):
    sdk = Mock()
    sdk.models.generate_content.side_effect = errors.ClientError(403, {'error': {'message': 'secret'}})
    client = GeminiLlm(api_key=SecretStr('secret'), model='test', sdk_client=sdk)
    with pytest.raises(LlmError) as caught:
        client.generate_json('private')
    assert sdk.models.generate_content.call_count == 1
    assert 'secret' not in str(caught.value)
    assert 'secret' not in caplog.text


def test_max_retries_zero_means_exactly_one_call():
    sdk = Mock()
    sdk.models.generate_content.side_effect = httpx.ConnectError('secret')
    sleeps = []
    client = GeminiLlm(api_key=SecretStr('secret'), model='test', max_retries=0,
                       sdk_client=sdk, sleep=sleeps.append)
    with pytest.raises(LlmError):
        client.generate_json('private')
    assert sdk.models.generate_content.call_count == 1
    assert sleeps == []


def test_ollama_request_contract_and_timeout():
    seen = []
    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={'message': {'content': '{"ok":true}'}})
    client = OllamaLlm(model='local-test', base_url='http://localhost:1234/',
                       timeout_seconds=7, transport=httpx.MockTransport(handle))
    assert client.generate_json('private') == '{"ok":true}'
    request = seen[0]
    assert str(request.url) == 'http://localhost:1234/api/chat'
    assert request.method == 'POST'
    assert json.loads(request.content) == {
        'model': 'local-test', 'messages': [{'role': 'user', 'content': 'private'}],
        'format': 'json', 'stream': False}
    assert request.extensions['timeout'] == {'connect': 7, 'read': 7, 'write': 7, 'pool': 7}
    client.close()


@pytest.mark.parametrize('payload', [[], {}, {'message': []}, {'message': {}}, {'message': {'content': 'invalid'}}])
def test_ollama_malformed_response_is_not_retried(payload):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=payload)
    client = OllamaLlm(model='test', transport=httpx.MockTransport(handle))
    with pytest.raises(LlmError):
        client.generate_json('private')
    assert len(calls) == 1


def test_gemini_initialization_is_sanitized(monkeypatch):
    def fail(**kwargs):
        raise ValueError('secret')
    monkeypatch.setattr('verbinden.llm.gemini.genai.Client', fail)
    with pytest.raises(LlmError, match='初期化') as caught:
        GeminiLlm(api_key=SecretStr('secret'), model='test')
    assert 'secret' not in str(caught.value)


def test_factory_requires_gemini_key(tmp_path):
    config = runtime(tmp_path, 'gemini').model_copy(update={'gemini_api_key': None})
    with pytest.raises(LlmError, match='GEMINI_API_KEY'):
        create_llm_client(config)


def test_factory_passes_retry_sleep(monkeypatch, tmp_path):
    sdk = Mock()
    sdk.models.generate_content.side_effect = [httpx.ReadTimeout('secret'), SimpleNamespace(text='{}')]
    monkeypatch.setattr('verbinden.llm.gemini.genai.Client', lambda **kwargs: sdk)
    sleeps = []
    client = create_llm_client(runtime(tmp_path, 'gemini'), sleep=sleeps.append)
    assert client.generate_json('private') == '{}'
    assert sleeps == [2]
    client.close()
    sdk.close.assert_called_once()
