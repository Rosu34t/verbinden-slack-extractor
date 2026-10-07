from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from pydantic import SecretStr

from verbinden.config import AppConfig, RuntimeConfig
from verbinden.llm import create_llm_client
from verbinden.llm.base import LlmClient, LlmError
from verbinden.llm.fake import FakeLlm
from verbinden.llm.gemini import GeminiLlm
from verbinden.llm.ollama import OllamaLlm


def runtime(tmp_path, provider):
    return RuntimeConfig(root=tmp_path, slack_bot_token=SecretStr('slack-test'),
        gemini_api_key=SecretStr('gemini-test'), config=AppConfig.model_validate({
        'env': 'test', 'slack': {'eventChannel': 'general', 'timesChannels': [], 'periodDays': 7},
        'llm': {'provider': provider, 'model': 'model-test', 'maxRetries': 3, 'timeoutSeconds': 9},
        'api': {'statusCacheSeconds': 60, 'corsOrigins': []}, 'output': {'members': {}},
        'paths': {'dataDir': 'data'}}))


def test_fake_records_prompts_as_tuple():
    client = FakeLlm('{"ok":true}')
    assert isinstance(client, LlmClient)
    assert client.generate_json('one') == '{"ok":true}'
    snapshot = client.prompts
    client.generate_json('two')
    assert snapshot == ('one',)
    assert client.prompts == ('one', 'two')


@pytest.mark.parametrize('provider,expected', [('fake', FakeLlm), ('gemini', GeminiLlm), ('ollama', OllamaLlm)])
def test_factory(tmp_path, provider, expected):
    client = create_llm_client(runtime(tmp_path, provider), sleep=lambda _: None)
    assert isinstance(client, expected)
    client.close()


def test_gemini_json_options_and_timeout():
    sdk = Mock()
    sdk.models.generate_content.return_value = SimpleNamespace(text='{}')
    client = GeminiLlm(api_key=SecretStr('secret'), model='test', timeout_seconds=7, sdk_client=sdk)
    assert client.generate_json('private prompt') == '{}'
    args = sdk.models.generate_content.call_args.kwargs
    assert args['model'] == 'test'
    assert args['contents'] == 'private prompt'
    assert args['config'].response_mime_type == 'application/json'
    assert args['config'].http_options.timeout == 7000
    assert args['config'].http_options.retry_options.attempts == 1


@pytest.mark.parametrize('status', [429, 500, 503])
def test_ollama_retries_http_status(status):
    calls = []
    sleeps = []
    def handle(request):
        calls.append(request)
        return httpx.Response(status if len(calls) == 1 else 200, json={'message': {'content': '{}'}})
    client = OllamaLlm(model='test', max_retries=3, transport=httpx.MockTransport(handle), sleep=sleeps.append)
    assert client.generate_json('private') == '{}'
    assert len(calls) == 2
    assert sleeps == [2]


def test_retry_exhaustion_is_sanitized():
    sleeps = []
    def handle(request):
        raise httpx.ReadTimeout('private prompt and key', request=request)
    client = OllamaLlm(model='test', max_retries=3, transport=httpx.MockTransport(handle), sleep=sleeps.append)
    with pytest.raises(LlmError) as caught:
        client.generate_json('private')
    assert sleeps == [2, 4, 8]
    assert 'private' not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize('status', [400, 401, 403, 404])
def test_nonretry_http_errors_are_sanitized(status):
    sleeps = []
    client = OllamaLlm(model='test', max_retries=3, sleep=sleeps.append,
        transport=httpx.MockTransport(lambda request: httpx.Response(status, text='private prompt and key')))
    with pytest.raises(LlmError) as caught:
        client.generate_json('private')
    assert sleeps == []
    assert 'private' not in str(caught.value)


@pytest.mark.parametrize('text', [None, '', '  ', 'not json'])
def test_gemini_bad_response_no_retry(text):
    sdk = Mock()
    sdk.models.generate_content.return_value = SimpleNamespace(text=text)
    sleeps = []
    client = GeminiLlm(api_key=SecretStr('secret'), model='test', sdk_client=sdk, sleep=sleeps.append)
    with pytest.raises(LlmError):
        client.generate_json('private')
    assert sdk.models.generate_content.call_count == 1
    assert sleeps == []
