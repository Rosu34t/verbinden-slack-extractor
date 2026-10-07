"""Offline refresh API contracts, with controlled workers and JST clocks."""
from datetime import datetime, timedelta
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from verbinden.api import create_app
from verbinden.batch import RefreshResult
from verbinden.batch_lock import BatchLock
from verbinden.config import AppConfig, RuntimeConfig
from verbinden.refresh import RefreshService, JST

class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 8, 10, tzinfo=JST)
    def __call__(self):
        return self.now
    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)

class Executor:
    def __init__(self):
        self.jobs = []
        self.closed = False
    def submit(self, function, *args):
        self.jobs.append((function, args))
    def run(self):
        function, args = self.jobs.pop(0)
        function(*args)
    def shutdown(self, wait=True):
        self.closed = True
        while self.jobs:
            self.run()

class Statuses:
    def get_statuses(self, ids):
        return {}

@pytest.fixture
def setup(tmp_path):
    config = AppConfig.model_validate({
        'env': 'test', 'slack': {'eventChannel': 'general', 'periodDays': 14, 'timesChannels': []},
        'llm': {'provider': 'fake', 'model': 'fake', 'maxRetries': 0, 'timeoutSeconds': 10},
        'api': {'statusCacheSeconds': 60, 'corsOrigins': ['http://localhost:3000']},
        'output': {'members': {}}, 'paths': {'dataDir': 'data'}})
    runtime = RuntimeConfig(config=config, root=tmp_path, slack_bot_token=SecretStr('fiction'))
    clock, executor = Clock(), Executor()
    calls = []
    def runner(target, lease):
        assert lease.held
        calls.append(target)
        return RefreshResult(target, 3, 1)
    service = RefreshService(runtime, runner=runner, clock=clock, executor=executor)
    return runtime, clock, executor, service, calls

def test_refresh_without_token_unknown_and_initial_status(setup):
    runtime, _, executor, service, _ = setup
    with TestClient(create_app(runtime, status_service=Statuses(), refresh_service=service)) as http:
        assert http.get('/api/refresh/status').json()['data']['events'] == {
            'state': 'idle', 'startedAt': None, 'finishedAt': None, 'nextAvailableAt': None, 'message': None}
        assert http.post('/api/refresh/unknown').status_code == 404
        assert http.post('/api/refresh/events').status_code == 202
        assert executor.jobs
        assert http.get('/api/health').status_code == 200
        for path in ('/api/refresh/events', '/api/refresh/members'):
            assert not http.get('/openapi.json').json()['paths'][path]['post'].get('parameters')

def test_running_success_target_cooldown_and_rounding(setup):
    runtime, clock, executor, service, calls = setup
    with TestClient(create_app(runtime, status_service=Statuses(), refresh_service=service)) as http:
        first = http.post('/api/refresh/events')
        assert first.status_code == 202
        assert first.json()['data']['state'] == 'running'
        assert executor.jobs and not calls
        for target in ('events', 'members'):
            busy = http.post('/api/refresh/' + target)
            assert busy.status_code == 409
            assert busy.json()['data']['state'] == ('running' if target == 'events' else 'idle')
        clock.advance(21)
        executor.run()
        done = http.get('/api/refresh/status').json()['data']['events']
        assert done['state'] == 'succeeded' and done['message'] == '更新しました'
        assert done['finishedAt'] == clock.now.isoformat()
        clock.advance(.25)
        cooldown = http.post('/api/refresh/events')
        assert cooldown.status_code == 429 and cooldown.headers['Retry-After'] == '579'
        assert cooldown.json()['data']['nextAvailableAt'] == '2026-10-08T10:10:00+09:00'
        assert http.post('/api/refresh/members').status_code == 202
        executor.run()
        clock.advance(579)
        assert http.get('/api/refresh/status').json()['data']['events']['nextAvailableAt'] is None
        assert http.post('/api/refresh/events').status_code == 202
    assert executor.closed

def test_failed_worker_preserves_fixed_message_and_releases_lock(setup, caplog):
    runtime, clock, executor, _, _ = setup
    def broken(target, lease):
        raise RuntimeError('PRIVATE_BODY_TOKEN')
    service = RefreshService(runtime, runner=broken, clock=clock, executor=executor)
    with TestClient(create_app(runtime, status_service=Statuses(), refresh_service=service)) as http:
        assert http.post('/api/refresh/events').status_code == 202
        executor.run()
        response = http.get('/api/refresh/status')
        assert response.json()['data']['events']['state'] == 'failed'
        assert response.json()['data']['events']['message'] == '更新に失敗しました。前のデータを表示しています'
        assert 'PRIVATE_BODY_TOKEN' not in response.text + caplog.text
        assert http.post('/api/refresh/events').status_code == 429
        assert http.post('/api/refresh/members').status_code == 202

def test_cli_lock_checked_before_cooldown(setup):
    runtime, clock, executor, service, _ = setup
    with TestClient(create_app(runtime, status_service=Statuses(), refresh_service=service)) as http:
        assert http.post('/api/refresh/events').status_code == 202
        executor.run()
        with BatchLock(runtime.root/'data', 'test', clock=clock):
            assert http.post('/api/refresh/events').status_code == 409
        assert http.post('/api/refresh/events').status_code == 429

def test_submit_failure_is_safe_and_releases_lease(setup, caplog):
    runtime, clock, _, _, _ = setup
    class Broken(Executor):
        def submit(self, *_):
            raise RuntimeError('PRIVATE_BODY_TOKEN')
    service = RefreshService(runtime, runner=lambda *_: None, clock=clock, executor=Broken())
    with TestClient(create_app(runtime, status_service=Statuses(), refresh_service=service)) as http:
        response = http.post('/api/refresh/events')
        assert response.status_code == 503
        assert response.json()['data']['state'] == 'failed'
        assert 'PRIVATE_BODY_TOKEN' not in response.text + caplog.text
        with BatchLock(runtime.root/'data', 'test', clock=clock):
            pass

def test_cors_content_type_and_unapproved_origin(setup):
    runtime, _, _, service, _ = setup
    with TestClient(create_app(runtime, status_service=Statuses(), refresh_service=service)) as http:
        headers = {'Origin': 'http://localhost:3000', 'Access-Control-Request-Method': 'POST',
                   'Access-Control-Request-Headers': 'Content-Type'}
        preflight = http.options('/api/refresh/events', headers=headers)
        assert preflight.status_code == 200
        assert 'content-type' in preflight.headers['Access-Control-Allow-Headers'].lower()
        assert http.options('/api/refresh/events', headers={**headers, 'Access-Control-Request-Headers': 'X-Refresh-Token'}).status_code == 400
        assert 'access-control-allow-credentials' not in preflight.headers
        assert http.options('/api/refresh/events', headers={**headers, 'Origin': 'http://other.test'}).status_code == 400
        assert http.options('/api/refresh/events', headers={**headers, 'Access-Control-Request-Method': 'DELETE'}).status_code == 400
        response = http.post('/api/refresh/events', headers={'Origin': 'http://localhost:3000'})
        assert response.status_code == 202
        assert response.headers['Access-Control-Allow-Origin'] == 'http://localhost:3000'
        assert 'X-Refresh-Token' not in http.get('/openapi.json').text


def test_cors_allows_json_content_type_and_exposes_retry_after(setup):
    runtime, _, _, service, _ = setup
    with TestClient(create_app(runtime, status_service=Statuses(), refresh_service=service)) as http:
        preflight = http.options('/api/refresh/events', headers={
            'Origin': 'http://localhost:3000', 'Access-Control-Request-Method': 'POST',
            'Access-Control-Request-Headers': 'content-type'})
        assert preflight.status_code == 200
        allowed = preflight.headers['Access-Control-Allow-Headers'].lower()
        assert 'content-type' in allowed and 'x-refresh-token' not in allowed

        response = http.post('/api/refresh/events', headers={'Origin': 'http://localhost:3000',
                                                             'Content-Type': 'application/json'})
        assert response.status_code == 202
        assert 'retry-after' in response.headers['Access-Control-Expose-Headers'].lower()

def test_zero_cooldown_service_without_token(setup):
    runtime, clock, executor, _, _ = setup
    config = runtime.config.model_copy(update={'api': runtime.config.api.model_copy(
        update={'refresh_cooldown_seconds': 0})})
    runtime = runtime.model_copy(update={'config': config})
    service = RefreshService(runtime, runner=lambda target, lease: RefreshResult(target, 0),
                             clock=clock, executor=executor)
    try:
        assert service.request('events')[0] == 202
        assert service.statuses()['events']['nextAvailableAt'] is None
        executor.run()
        assert service.request('events')[0] == 202
    finally:
        service.close()


def test_concurrent_requests_accept_only_one_worker(setup):
    from concurrent.futures import ThreadPoolExecutor
    runtime, _, executor, service, _ = setup
    with ThreadPoolExecutor(max_workers=6) as callers:
        codes = list(callers.map(lambda _: service.request('events')[0], range(12)))
    assert codes.count(202) == 1 and codes.count(409) == 11
    assert len(executor.jobs) == 1
    service.close()


def test_real_executor_runs_off_thread_and_shutdown_waits(setup):
    from threading import Event
    runtime, clock, _, _, _ = setup
    entered, proceed = Event(), Event()
    def runner(target, lease):
        entered.set()
        assert proceed.wait(3)
        return RefreshResult(target, 0)
    service = RefreshService(runtime, runner=runner, clock=clock)
    try:
        assert service.request('events')[0] == 202
        assert entered.wait(3)
        assert service.statuses()['events']['state'] == 'running'
        assert service.request('members')[0] == 409
    finally:
        proceed.set()
        service.close()
    assert service.statuses()['events']['state'] == 'succeeded'


@pytest.mark.parametrize('target', ['events', 'members'])
@pytest.mark.parametrize('fails', [False, True])
def test_default_runner_uses_fresh_dependencies_and_safe_cleanup(setup, monkeypatch, caplog, target, fails):
    from verbinden import refresh
    runtime, clock, _, _, _ = setup
    seen = []
    gateway = object()
    class Llm:
        def close(self):
            seen.append('closed')
            raise RuntimeError('PRIVATE_CLOSE_TOKEN')
    llm = Llm()
    monkeypatch.setattr(refresh, 'create_gateway', lambda value: gateway)
    monkeypatch.setattr(refresh, 'create_llm_client', lambda value: llm)
    def worker(value, **options):
        assert value is runtime and options['gateway'] is gateway and options['llm'] is llm
        assert options['_lease'].held and options['clock'] is clock
        assert isinstance(options['storage'], __import__('verbinden.storage', fromlist=['Storage']).Storage)
        seen.append(target)
        if fails:
            raise ValueError('PRIVATE_JOB_TOKEN')
        return RefreshResult(target, 4)
    monkeypatch.setattr(refresh, 'refresh_' + target, worker)
    with BatchLock(runtime.root/'data', 'test', clock=clock) as lease:
        if fails:
            with pytest.raises(ValueError, match='PRIVATE_JOB_TOKEN'):
                refresh.run_refresh(runtime, target, lease, clock=clock)
        else:
            assert refresh.run_refresh(runtime, target, lease, clock=clock).count == 4
    assert seen == [target, 'closed']
    assert 'PRIVATE_CLOSE_TOKEN' not in caplog.text and 'PRIVATE_JOB_TOKEN' not in caplog.text


def test_lock_io_failure_does_not_leak_or_start_cooldown(setup, monkeypatch, caplog):
    from verbinden import refresh
    _, _, executor, service, _ = setup
    def broken(*_):
        raise OSError('PRIVATE_PATH_TOKEN')
    monkeypatch.setattr(refresh.BatchLock, 'acquire', broken)
    code, payload, _ = service.request('events')
    assert code == 503 and payload['data']['state'] == 'idle'
    assert service.statuses()['events']['nextAvailableAt'] is None
    assert 'PRIVATE_PATH_TOKEN' not in str(payload) + caplog.text
    assert not executor.jobs
    service.close()
