"""HTTP contracts tested offline using temporary output and fake status lookup."""
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from verbinden.config import AppConfig, ConfigError, RuntimeConfig
from verbinden.models import LiveStatus, Profile
from verbinden.storage import Storage
from verbinden import api


@pytest.fixture
def runtime(tmp_path):
    config = AppConfig.model_validate({
        'env': 'test', 'slack': {'eventChannel': 'general', 'periodDays': 14, 'timesChannels': []},
        'llm': {'provider': 'fake', 'model': 'fake', 'maxRetries': 0, 'timeoutSeconds': 10},
        'api': {'statusCacheSeconds': 60, 'corsOrigins': ['http://localhost:3000']},
        'output': {'members': {'id': 'id', 'name': 'nickname', 'status': 'status'}},
        'paths': {'dataDir': 'data'}})
    return RuntimeConfig(config=config, root=tmp_path, slack_bot_token=SecretStr('fiction'))


class Statuses:
    def get_statuses(self, ids):
        return {key: LiveStatus(presence='active', text='工作中', emoji='🔨') for key in ids}


def profile():
    return Profile(id='mem-U1', user_id='U1', display_name='架空', hobbies=[], recent_work=[],
                   active_hours=[0]*24, post_count=0, updated_at='2026-10-07')


def event(day='2026-10-08'):
    return {'id': 'slk123', 'summary': '架空イベント', 'description': '説明',
            'start': {'date': day}, 'end': {'date': '2026-10-12'}}


def client(runtime, status=None):
    return TestClient(api.create_app(runtime, status_service=status or Statuses()))


def test_health_is_independent_of_files(runtime):
    with client(runtime) as http:
        assert http.get('/api/health').json() == {'success': True, 'data': {'env': 'test'}, 'error': None}


def test_members_fieldmap_status_and_independent_file(runtime):
    store = Storage(runtime.root/'data')
    store.save_out('test', [profile()], [])
    (runtime.root/'data/out/test/events.json').write_text('broken')
    with client(runtime) as http:
        result = http.get('/api/members')
        assert result.status_code == 200
        assert result.json()['data'] == [{'id': 'mem-U1', 'nickname': '架空',
            'status': {'presence': 'active', 'emoji': '🔨', 'text': '工作中'}}]
        assert result.json()['error'] is None


def test_events_sorted_and_independent_file(runtime):
    store = Storage(runtime.root/'data')
    store.save_out('test', [], [event('2026-10-10'), event()])
    (runtime.root/'data/out/test/profiles.json').write_text('broken')
    with client(runtime) as http:
        response = http.get('/api/events')
        assert response.status_code == 200
        assert response.json()['data'] == [event(), event('2026-10-10')]


@pytest.mark.parametrize('endpoint', ['members', 'events'])
@pytest.mark.parametrize('content', [None, '{broken', '{"token":"SECRET"}', 'null',
                                     '[NaN]', '[Infinity]', '[-Infinity]'])
def test_missing_or_corrupt_data_safe_503(runtime, endpoint, content, caplog):
    path = runtime.root/'data/out/test'/('profiles.json' if endpoint == 'members' else 'events.json')
    if content:
        path.parent.mkdir(parents=True)
        path.write_text(content)
    with client(runtime) as http:
        response = http.get('/api/'+endpoint)
    assert response.status_code == 503
    assert response.json() == {'success': False, 'data': None,
        'error': 'データがまだ作られていません。バッチを実行してください'}
    assert 'SECRET' not in response.text + caplog.text


@pytest.mark.parametrize('bad', [
    {'start': {'date': 'not-a-date'}}, {'end': {'date': '2026-10-07'}},
    {'start': {'dateTime': '2026-10-08T12:00:00'}},
    {'start': {'date': '2026-10-08', 'dateTime': '2026-10-08T12:00:00+09:00'}},
    {'summary': 123}, {'location': {'unsafe': True}}, {'summary': ''}, {'id': ''},
    {'start': {'date': '2026-02-30'}}, {'description': None},
    {'end': {'dateTime': '2026-10-09T12:00:00+09:00', 'timeZone': 'Asia/Tokyo'}},
])
def test_calendar_invalid_shape_is_skipped(runtime, bad, caplog):
    Storage(runtime.root/'data').save_out('test', [], [event(), {**event(), **bad}])
    with client(runtime) as http:
        response = http.get('/api/events')
    assert response.status_code == 200
    assert response.json() == {'success': True, 'data': [event()], 'error': None}
    assert 'api invalid events skipped: count=1' in caplog.text


def test_events_skip_invalid_objects_and_scalars_without_disclosing_content(runtime, caplog):
    path = runtime.root/'data/out/test/events.json'
    path.parent.mkdir(parents=True)
    invalid = {'id': 'PRIVATE_ID', 'summary': 'SECRET_POST', 'token': 'SECRET_TOKEN'}
    path.write_text(json.dumps([event('2026-10-10'), invalid, None, 'SECRET_SCALAR',
                               4, [], event()]))
    with client(runtime) as http:
        response = http.get('/api/events')
    assert response.status_code == 200
    assert response.json() == {'success': True, 'data': [event(), event('2026-10-10')], 'error': None}
    assert 'api invalid events skipped: count=5' in caplog.text
    for secret in ('PRIVATE_ID', 'SECRET_POST', 'SECRET_TOKEN', 'SECRET_SCALAR'):
        assert secret not in response.text + caplog.text


def test_all_invalid_events_return_empty_success(runtime, caplog):
    Storage(runtime.root/'data').save_out('test', [], [{'token': 'SECRET'}])
    with client(runtime) as http:
        response = http.get('/api/events')
    assert response.status_code == 200
    assert response.json() == {'success': True, 'data': [], 'error': None}
    assert 'api invalid events skipped: count=1' in caplog.text
    assert 'SECRET' not in response.text + caplog.text


def test_mixed_timed_and_all_day_order(runtime):
    timed = {**event(), 'start': {'dateTime': '2026-10-08T09:00:00+09:00', 'timeZone': 'Asia/Tokyo'},
             'end': {'dateTime': '2026-10-08T10:00:00+09:00', 'timeZone': 'Asia/Tokyo'}}
    Storage(runtime.root/'data').save_out('test', [], [timed, event()])
    with client(runtime) as http:
        assert http.get('/api/events').json()['data'] == [event(), timed]


def test_total_status_failure_falls_back(runtime, caplog):
    class Broken:
        def get_statuses(self, ids):
            raise RuntimeError('SECRET')
    Storage(runtime.root/'data').save_out('test', [profile()], [])
    with client(runtime, Broken()) as http:
        result = http.get('/api/members')
    assert result.status_code == 200
    assert result.json()['data'][0]['status'] == {'presence': 'away', 'text': None, 'emoji': None}
    assert 'SECRET' not in caplog.text


def test_cors_allows_only_configured_origin_and_get_post(runtime):
    with client(runtime) as http:
        allowed = http.get('/api/health', headers={'Origin': 'http://localhost:3000'})
        assert allowed.headers['access-control-allow-origin'] == 'http://localhost:3000'
        blocked = http.get('/api/health', headers={'Origin': 'https://other.test'})
        assert 'access-control-allow-origin' not in blocked.headers
        for method, code in [('GET', 200), ('POST', 200)]:
            preflight = http.options('/api/health', headers={'Origin': 'http://localhost:3000',
                'Access-Control-Request-Method': method})
            assert preflight.status_code == code
        assert http.post('/api/health').status_code == 405


def test_startup_loads_selected_env_and_safe_failure(runtime, monkeypatch, caplog):
    seen = []
    def load(env):
        seen.append(env)
        return runtime
    monkeypatch.setenv('VERBINDEN_ENV', 'prod')
    monkeypatch.setattr(api, 'load_config', load)
    monkeypatch.setattr(api, 'create_status_service', lambda _: Statuses())
    with TestClient(api.create_app()) as http:
        assert http.get('/api/health').status_code == 200
    assert seen == ['prod']
    def broken(env):
        raise ConfigError('SECRET')
    monkeypatch.setattr(api, 'load_config', broken)
    with pytest.raises(RuntimeError, match='設定を確認'):
        with TestClient(api.create_app()):
            pass
    assert 'SECRET' not in caplog.text


def test_empty_outputs_and_no_status_lookups(runtime):
    class EmptyStatuses:
        def get_statuses(self, ids):
            assert ids == []
            return {}
    Storage(runtime.root/'data').save_out('test', [], [])
    with client(runtime, EmptyStatuses()) as http:
        for endpoint in ('members', 'events'):
            assert http.get('/api/'+endpoint).json() == {'success': True, 'data': [], 'error': None}
        for method in ('put', 'patch', 'delete', 'head'):
            assert getattr(http, method)('/api/events').status_code == 405


def test_partial_status_failure_keeps_other_members(runtime):
    second = profile().model_copy(update={'id': 'mem-U2', 'user_id': 'U2'})
    class Partial:
        def get_statuses(self, ids):
            assert ids == ['U1', 'U2']
            return {'U1': LiveStatus(presence='active'), 'U2': None}
    Storage(runtime.root/'data').save_out('test', [profile(), second], [])
    with client(runtime, Partial()) as http:
        values = http.get('/api/members').json()['data']
        assert [item['status']['presence'] for item in values] == ['active', 'away']


def test_default_env_and_cors_loaded_at_startup(runtime, monkeypatch):
    seen = []
    monkeypatch.delenv('VERBINDEN_ENV', raising=False)
    def load(env):
        seen.append(env)
        return runtime
    monkeypatch.setattr(api, 'load_config', load)
    monkeypatch.setattr(api, 'create_status_service', lambda _: Statuses())
    with TestClient(api.create_app()) as http:
        response = http.get('/api/health', headers={'Origin': 'http://localhost:3000'})
        assert response.headers['access-control-allow-origin'] == 'http://localhost:3000'
    assert seen == ['test']


def test_status_factory_constructs_read_only_gateway(runtime, monkeypatch):
    calls = []
    class FakeClient:
        def __init__(self, token):
            assert token == 'fiction'
            calls.append('client')
    class FakeGateway:
        def __init__(self, web_client, env):
            assert isinstance(web_client, FakeClient) and env == 'test'
            calls.append('gateway')
    class FakeStatus:
        def __init__(self, gateway, cache_seconds):
            assert isinstance(gateway, FakeGateway) and cache_seconds == 60
            calls.append('status')
    monkeypatch.setattr(api, 'WebClient', FakeClient)
    monkeypatch.setattr(api, 'SlackGateway', FakeGateway)
    monkeypatch.setattr(api, 'LiveStatusService', FakeStatus)
    assert isinstance(api.create_status_service(runtime), FakeStatus)
    assert calls == ['client', 'gateway', 'status']


def test_documentation_and_openapi_are_available(runtime):
    with client(runtime) as http:
        docs = http.get('/docs')
        assert docs.status_code == 200
        assert 'Swagger UI' in docs.text
        schema = http.get('/openapi.json')
        assert schema.status_code == 200
        assert set(schema.json()['paths']) == {'/api/health', '/api/members', '/api/events',
            '/api/refresh/events', '/api/refresh/members', '/api/refresh/status'}
        assert all(set(methods) == ({'post'} if path in {'/api/refresh/events', '/api/refresh/members'} else {'get'})
                   for path, methods in schema.json()['paths'].items())
