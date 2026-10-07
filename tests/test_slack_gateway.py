from datetime import datetime, timezone
import logging

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

from verbinden.slack_gateway import LiveStatusService, SlackGateway, SlackGatewayError


class FakeWebClient:
    def __init__(self, **responses):
        self.responses = responses
        self.calls = []
        self.retry_handlers = []
        self._logger = logging.getLogger('fake-slack')

    def __getattr__(self, method):
        def call(**kwargs):
            self.calls.append((method, kwargs))
            result = self.responses[method].pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return call


def error(code):
    return SlackApiError('SECRET RAW POST', {'error': code})


def test_channels_page_public_and_match():
    client = FakeWebClient(conversations_list=[
        {'channels': [{'id': 'C1', 'name': 'other'}], 'response_metadata': {'next_cursor': 'next'}},
        {'channels': [{'id': 'C2', 'name': 'general'}]},
    ])
    gateway = SlackGateway(client)
    assert gateway.find_channel('general')['id'] == 'C2'
    assert client.calls[0][1]['types'] == 'public_channel'
    assert client.calls[1][1]['cursor'] == 'next'
    assert any(isinstance(h, RateLimitErrorRetryHandler) for h in client.retry_handlers)


def test_history_pages_parent_and_broadcast():
    client = FakeWebClient(conversations_history=[
        {'messages': [{'ts': '1', 'text': 'parent'}, {'ts': '2', 'thread_ts': '1'}],
         'response_metadata': {'next_cursor': 'n'}},
        {'messages': [{'ts': '3', 'thread_ts': '1', 'subtype': 'thread_broadcast'},
                      {'ts': '4', 'thread_ts': '4'}]},
    ])
    results = SlackGateway(client).fetch_history('C1', datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert [r['ts'] for r in results] == ['1', '4']
    assert client.calls[0][1]['oldest'] == str(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())


def test_owner_override_and_creator():
    client = FakeWebClient(conversations_info=[{'channel': {'creator': 'U1'}}])
    gateway = SlackGateway(client)
    assert gateway.resolve_owner('C1', 'U2') == 'U2'
    assert client.calls == []
    assert gateway.resolve_owner('C1', None) == 'U1'


def test_user_presence():
    client = FakeWebClient(users_info=[{'user': {'id': 'U1'}}], users_getPresence=[{'presence': 'active'}])
    gateway = SlackGateway(client)
    assert gateway.fetch_user('U1') == {'id': 'U1'}
    assert gateway.fetch_presence('U1') == 'active'


@pytest.mark.parametrize('code,fragment', [('channel_not_found', '見つかりません'),
    ('not_in_channel', '/invite'), ('invalid_auth', '.env.test'), ('mystery', '失敗')])
def test_errors_sanitized(code, fragment):
    client = FakeWebClient(conversations_list=[error(code)])
    with pytest.raises(SlackGatewayError) as caught:
        SlackGateway(client).find_channel('general')
    assert fragment in str(caught.value)
    assert 'SECRET' not in str(caught.value)
    assert caught.value.__suppress_context__


def test_missing_channel():
    with pytest.raises(SlackGatewayError, match='#general'):
        SlackGateway(FakeWebClient(conversations_list=[{'channels': []}])).find_channel('general')


@pytest.mark.parametrize('expiration,expected', [(None, 'hello'), (0, 'hello'), (99, None), (100, None), (101, 'hello')])
def test_status_expiration(expiration, expected):
    client = FakeWebClient(users_info=[{'user': {'profile': {'status_text': 'hello', 'status_emoji': ':smile:',
                                                             'status_expiration': expiration}}}],
                           users_getPresence=[{'presence': 'active'}])
    status = LiveStatusService(SlackGateway(client), 60, lambda: 100).get_statuses(['U1'])['U1']
    assert status.text == expected
    assert status.emoji == ('😄' if expected else None)


def test_cache_user_set_and_expiry():
    now = [100.0]
    client = FakeWebClient(users_info=[{'user': {'profile': {'status_emoji': ':custom:'}}}] * 3,
                           users_getPresence=[{'presence': 'away'}] * 3)
    service = LiveStatusService(SlackGateway(client), 10, lambda: now[0])
    first = service.get_statuses(['U1'])
    assert first['U1'].emoji == ':custom:'
    first.clear()
    assert service.get_statuses(['U1', 'U2'])['U1'] is not None
    assert len(client.calls) == 4
    now[0] = 110
    service.get_statuses(['U1'])
    assert len(client.calls) == 6


def test_partial_failure_and_cache():
    client = FakeWebClient(users_info=[error('invalid_auth'), {'user': {'profile': {}}}],
                           users_getPresence=[{'presence': 'away'}])
    service = LiveStatusService(SlackGateway(client), 60, lambda: 100)
    result = service.get_statuses(['bad', 'good'])
    assert result['bad'] is None
    assert result['good'].presence == 'away'
    assert service.get_statuses(['bad']) == {'bad': None}


def test_naive_datetime_rejected():
    with pytest.raises(ValueError, match='timezone'):
        SlackGateway(FakeWebClient()).fetch_history('C1', datetime(2026, 1, 1))


def test_sdk_logger_disabled_and_generic_error_sanitized():
    client = FakeWebClient(users_info=[RuntimeError('SECRET TOKEN')])
    gateway = SlackGateway(client)
    assert client._logger.disabled
    with pytest.raises(SlackGatewayError) as caught:
        gateway.fetch_user('U1')
    assert 'SECRET' not in str(caught.value)


def test_repeat_cursor_stops():
    client = FakeWebClient(conversations_list=[{'response_metadata': {'next_cursor': 'n'}}] * 2)
    with pytest.raises(SlackGatewayError, match='ページ送り'):
        SlackGateway(client).find_channel('general')


def test_existing_retry_handler_not_duplicated():
    client = FakeWebClient()
    client.retry_handlers = [RateLimitErrorRetryHandler()]
    SlackGateway(client)
    assert len(client.retry_handlers) == 1


@pytest.mark.parametrize('kwargs', [{'env': 'unknown'}, {'env': '.env/secret'}])
def test_invalid_environment(kwargs):
    with pytest.raises(ValueError):
        SlackGateway(FakeWebClient(), **kwargs)


@pytest.mark.parametrize('value', [-1, True, 1.1])
def test_invalid_cache_seconds(value):
    with pytest.raises(ValueError):
        LiveStatusService(SlackGateway(FakeWebClient()), value)


@pytest.mark.parametrize('method,args', [('find_channel', ('#general',)), ('fetch_user', ('',)),
    ('fetch_presence', ('',)), ('resolve_owner', ('',)), ('resolve_owner', ('C1', ''))])
def test_invalid_identifiers(method, args):
    with pytest.raises(ValueError):
        getattr(SlackGateway(FakeWebClient()), method)(*args)


def test_invalid_presence_and_missing_creator():
    gateway = SlackGateway(FakeWebClient(users_getPresence=[{'presence': 'other'}], conversations_info=[{'channel': {}}]))
    with pytest.raises(SlackGatewayError, match='在席'):
        gateway.fetch_presence('U1')
    with pytest.raises(SlackGatewayError, match='作成者'):
        gateway.resolve_owner('C1')


def test_channel_error_uses_resolved_name():
    client = FakeWebClient(conversations_list=[{'channels': [{'id': 'C1', 'name': 'general'}]}],
                           conversations_history=[error('not_in_channel')])
    gateway = SlackGateway(client)
    gateway.find_channel('general')
    with pytest.raises(SlackGatewayError, match='#general'):
        gateway.fetch_history('C1', datetime.now(timezone.utc))


def test_status_expiration_within_cache_ttl():
    now = [100]
    client = FakeWebClient(users_info=[{'user': {'profile': {'status_text': 'hello', 'status_expiration': 105}}}],
                           users_getPresence=[{'presence': 'away'}])
    service = LiveStatusService(SlackGateway(client), 60, lambda: now[0])
    assert service.get_statuses(['U1'])['U1'].text == 'hello'
    now[0] = 106
    assert service.get_statuses(['U1'])['U1'].text is None
