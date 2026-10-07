"""Offline batch contract tests; all external access uses fakes."""
import json
from datetime import datetime, timezone, timedelta

import pytest
from pydantic import SecretStr

from verbinden.config import AppConfig, RuntimeConfig
from verbinden.storage import Storage
from verbinden.llm import FakeLlm
from verbinden import batch

NOW = datetime(2026, 10, 7, 12, tzinfo=timezone(timedelta(hours=9)))


@pytest.fixture
def runtime(tmp_path):
    config = AppConfig.model_validate({
        'env': 'test', 'slack': {'eventChannel': 'general', 'periodDays': 14,
            'timesChannels': [{'channel': 'times-a'}, {'channel': 'times-b'},
                              {'channel': 'times-c', 'owner': 'U2'}]},
        'llm': {'provider': 'fake', 'model': 'fake', 'maxRetries': 0, 'timeoutSeconds': 10},
        'api': {'statusCacheSeconds': 60, 'corsOrigins': []},
        'output': {'members': {'name': 'name'}}, 'paths': {'dataDir': 'data'}})
    return RuntimeConfig(config=config, root=tmp_path, slack_bot_token=SecretStr('fiction'))


def message(ts='1791320000.000001', user='U1', text='工作をしました', **extra):
    return {'ts': ts, 'user': user, 'text': text, **extra}


class Gateway:
    def __init__(self, raw=None):
        self.raw = raw or {'general': [], 'times-a': [], 'times-b': [], 'times-c': []}
        self.calls = []

    def find_channel(self, name):
        self.calls.append(('find', name))
        return {'id': name, 'name': name}

    def resolve_owner(self, channel_id, override=None):
        return override or 'U1'

    def fetch_history(self, channel_id, oldest):
        self.calls.append(('history', channel_id, oldest))
        return self.raw[channel_id]

    def fetch_user(self, user_id):
        self.calls.append(('user', user_id))
        return {'id': user_id, 'profile': {'display_name': user_id, 'image_192': 'https://example.test/icon'}}


def test_owner_channels_merge_deduplicate_exclude_friends_replies_bots(runtime):
    raw = {'general': [], 'times-a': [message(reactions=[{'users': ['FRIEND']}]),
        message('1791320001.000001', user='FRIEND', text='友人だけの文章'),
        message('1791320002.000001', thread_ts='1791320000.000001', text='返信だけの文章'),
        message('1791320003.000001', bot_id='B1', text='Botの文章')],
        'times-b': [message(), message('1791320004.000001', text='新しい作品')], 'times-c': []}
    llm = FakeLlm('{"intro":"工作が好きです"}')
    result = batch.run_batch(runtime, gateway=Gateway(raw), llm=llm, clock=lambda: NOW)
    profiles, _ = Storage(runtime.root / 'data').load_out('test')
    assert result.profile_count == 2
    assert profiles[0].user_id == 'U1' and profiles[0].post_count == 2
    assert sum(profiles[0].active_hours) == 2
    assert profiles[1].intro is None and profiles[1].post_count == 0
    assert len(llm.prompts) == 1
    assert all(word not in llm.prompts[0] for word in ['友人だけ', '返信だけ', 'Botの文章', 'FRIEND'])
    assert llm.prompts[0].index('1791320004') < llm.prompts[0].index('1791320000')


def test_output_never_constructs_or_calls_network(runtime, monkeypatch):
    storage = Storage(runtime.root / 'data')
    storage.save_extracted('test', [], [])
    def forbidden(*args, **kwargs):
        pytest.fail('output stage constructed external service')
    monkeypatch.setattr(batch, 'create_gateway', forbidden)
    monkeypatch.setattr(batch, 'create_llm_client', forbidden)
    result = batch.run_batch(runtime, from_stage='output', clock=lambda: NOW)
    assert result.event_count == result.profile_count == 0
    assert storage.load_out('test') == ([], [])


def test_extract_reuses_raw_without_fetch_history(runtime):
    storage = Storage(runtime.root / 'data', clock=lambda: NOW)
    for channel in ('general', 'times-a', 'times-b', 'times-c'):
        storage.save_raw('test', channel, [])
    gateway = Gateway()
    batch.run_batch(runtime, 'extract', gateway=gateway, llm=FakeLlm(), clock=lambda: NOW)
    assert not any(call[0] == 'history' for call in gateway.calls)


def test_one_profile_llm_failure_does_not_stop_other_owner(runtime, caplog):
    class Llm:
        calls = 0
        def generate_json(self, prompt):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError('SECRET body token')
            return '{"intro":"読書が好きです"}'
    gateway = Gateway({'general': [], 'times-a': [message()], 'times-b': [],
                       'times-c': [message(user='U2')]})
    batch.run_batch(runtime, gateway=gateway, llm=Llm(), clock=lambda: NOW)
    profiles, _ = Storage(runtime.root / 'data').load_out('test')
    assert profiles[0].intro is None and profiles[1].intro == '読書が好きです'
    assert 'SECRET' not in caplog.text and 'body token' not in caplog.text


def test_event_uses_verified_source_and_saves_calendar(runtime):
    raw = {'general': [message(text='明日イベント')], 'times-a': [], 'times-b': [], 'times-c': []}
    llm = FakeLlm(json.dumps({'events': [{'sourcePostId': '1791320000.000001',
        'title': '架空イベント', 'date': '2026-10-08', 'sourceChannel': 'forged'}]}))
    batch.run_batch(runtime, gateway=Gateway(raw), llm=llm, clock=lambda: NOW)
    _, events = Storage(runtime.root / 'data').load_out('test')
    assert 'Slack #general' in events[0]['description']


@pytest.mark.parametrize('stage', ['fetch', 'extract', 'output'])
def test_cli_passes_stage(runtime, monkeypatch, stage):
    calls = []
    monkeypatch.setattr(batch, 'load_config', lambda env: runtime)
    monkeypatch.setattr(batch, 'run_batch', lambda config, from_stage: calls.append((config, from_stage)))
    assert batch.main(['--env', 'test', '--from-stage', stage]) == 0
    assert calls == [(runtime, stage)]


def test_cli_failure_is_sanitized(monkeypatch, caplog):
    def fail(env):
        raise RuntimeError('PRIVATE BODY token')
    monkeypatch.setattr(batch, 'load_config', fail)
    assert batch.main([]) == 1
    assert 'PRIVATE' not in caplog.text
    assert 'バッチに失敗' in caplog.text


def test_invalid_stage_rejected_before_external_factories(runtime):
    with pytest.raises(ValueError):
        batch.run_batch(runtime, 'invalid')


def test_raw_keeps_only_text_metadata_without_reactions_or_files(runtime):
    raw = {'general': [], 'times-a': [message(reactions=[{'name': 'heart'}],
                files=[{'private': 'not saved'}])], 'times-b': [], 'times-c': []}
    batch.run_batch(runtime, gateway=Gateway(raw), llm=FakeLlm(), clock=lambda: NOW)
    saved = Storage(runtime.root / 'data').load_latest_raw('test')['times-a'][0]
    assert set(saved) == {'ts', 'user', 'text'}


def test_friend_mentions_are_masked_without_loading_friend_info(runtime):
    raw = {'general': [], 'times-a': [message(text='<@FRIEND> ありがとう')],
           'times-b': [], 'times-c': []}
    gateway = Gateway(raw)
    llm = FakeLlm()
    batch.run_batch(runtime, gateway=gateway, llm=llm, clock=lambda: NOW)
    assert '@メンバー' in llm.prompts[0] and 'FRIEND' not in llm.prompts[0]
    assert ('user', 'FRIEND') not in gateway.calls


def test_factories_and_owned_llm_closed_on_success(runtime, monkeypatch):
    class Llm(FakeLlm):
        closed = False
        def close(self):
            self.closed = True
    llm = Llm()
    monkeypatch.setattr(batch, 'create_gateway', lambda config: Gateway())
    monkeypatch.setattr(batch, 'create_llm_client', lambda config: llm)
    batch.run_batch(runtime, clock=lambda: NOW)
    assert llm.closed


def test_owned_llm_closed_after_event_failure(runtime, monkeypatch):
    class Llm:
        closed = False
        def generate_json(self, prompt):
            raise RuntimeError('private provider body')
        def close(self):
            self.closed = True
    llm = Llm()
    monkeypatch.setattr(batch, 'create_llm_client', lambda config: llm)
    gateway = Gateway({'general': [message()], 'times-a': [], 'times-b': [], 'times-c': []})
    with pytest.raises(RuntimeError):
        batch.run_batch(runtime, gateway=gateway, clock=lambda: NOW)
    assert llm.closed


def test_owned_bot_posts_do_not_count(runtime):
    class BotGateway(Gateway):
        def fetch_user(self, user_id):
            return {**super().fetch_user(user_id), 'is_bot': True}
    gateway = BotGateway({'general': [message()], 'times-a': [message()],
                          'times-b': [], 'times-c': []})
    llm = FakeLlm()
    batch.run_batch(runtime, gateway=gateway, llm=llm, clock=lambda: NOW)
    assert not llm.prompts
    assert all(profile.post_count == 0 for profile in Storage(runtime.root / 'data').load_out('test')[0])


def test_clock_requires_jst(runtime):
    with pytest.raises(ValueError, match='JST'):
        batch.run_batch(runtime, clock=lambda: NOW.replace(tzinfo=None))


def test_invalid_profile_fields_are_reported_without_payload(runtime, caplog):
    raw = {'general': [], 'times-a': [message()], 'times-b': [], 'times-c': []}
    batch.run_batch(runtime, gateway=Gateway(raw), llm=FakeLlm('{"intro":123}'), clock=lambda: NOW)
    assert '不正項目=1' in caplog.text
    assert '123' not in caplog.text


def test_gateway_factory_constructs_with_runtime_environment(runtime, monkeypatch):
    seen = []
    client = object()
    def fake_client(*, token):
        seen.append(token)
        return client
    monkeypatch.setattr(batch, 'WebClient', fake_client)
    monkeypatch.setattr(batch, 'SlackGateway', lambda received, env: (received, env))
    assert batch.create_gateway(runtime) == (client, 'test')
    assert seen == ['fiction']


def test_file_share_text_counts_only_owner_and_friend_display_name_never_loaded(runtime):
    class NamedGateway(Gateway):
        def fetch_user(self, user_id):
            if user_id == 'FRIEND':
                pytest.fail('friend user info must not be read for profiles')
            return {**super().fetch_user(user_id),
                    'profile': {'display_name': '本人', 'image_192': None}}
    gateway = NamedGateway({'general': [], 'times-a': [
        message(subtype='file_share', text='<@FRIEND> ありがとう', files=[{'id': 'F1'}]),
        message('1791320001.000001', user='FRIEND', subtype='file_share', text='友人の文章'),
        message('1791320002.000001', subtype='file_share', text='', files=[{'id': 'F2'}])],
        'times-b': [], 'times-c': []})
    llm = FakeLlm()
    batch.run_batch(runtime, gateway=gateway, llm=llm, clock=lambda: NOW)
    profile = Storage(runtime.root / 'data').load_out('test')[0][0]
    assert profile.post_count == 1
    assert '@メンバー' in llm.prompts[0]
    assert 'FRIEND' not in llm.prompts[0]


def test_merged_profile_masks_ambiguous_friend_mentions_without_changing_activity_counts(runtime):
    raw = {'general': [],
           'times-a': [message(text='@Alice Smith ありがとう。作品を制作しました。')],
           'times-b': [message('1791320004.000001', text='@Bob Brown 感謝します\n<@U1> 作業中')],
           'times-c': []}
    llm = FakeLlm('{"intro":"制作を楽しんでいます"}')
    batch.run_batch(runtime, gateway=Gateway(raw), llm=llm, clock=lambda: NOW)
    profiles, _ = Storage(runtime.root / 'data').load_out('test')
    owner = next(item for item in profiles if item.user_id == 'U1')
    assert owner.post_count == sum(owner.active_hours) == 2
    assert len(llm.prompts) == 1
    assert not any(name in llm.prompts[0] for name in ['Alice', 'Smith', 'Bob', 'Brown', 'ありがとう', '感謝します'])
    assert '@メンバー。作品を制作しました。' in llm.prompts[0]
    assert '@U1 作業中' in llm.prompts[0]


def test_native_owner_identity_is_preserved_but_ambiguous_owner_prefix_friend_name_is_masked(runtime):
    class NamedGateway(Gateway):
        def fetch_user(self, user_id):
            user = super().fetch_user(user_id)
            return {**user, 'profile': {**user['profile'], 'display_name': 'Alice' if user_id == 'U1' else user_id}}
    text = '<@U1> 作業中。@Alice Smith ありがとう。作品を公開。'
    raw = {'general': [], 'times-a': [message(text=text)], 'times-b': [], 'times-c': []}
    llm = FakeLlm('{"intro":"制作が好きです"}')
    batch.run_batch(runtime, gateway=NamedGateway(raw), llm=llm, clock=lambda: NOW)
    assert '@Alice 作業中。@メンバー。作品を公開。' in llm.prompts[0]
    assert 'Smith' not in llm.prompts[0]
    assert raw['times-a'][0]['text'] == text
    assert Storage(runtime.root / 'data').load_latest_raw('test')['times-a'][0]['text'] == text


def test_owner_mention_placeholder_does_not_rewrite_original_text(runtime):
    marker = '\ue000OWN_MENTION\ue001'
    text = marker + ' <@U1> 作業中。'
    raw = {'general': [], 'times-a': [message(text=text)], 'times-b': [], 'times-c': []}
    llm = FakeLlm('{"intro":"制作が好きです"}')
    batch.run_batch(runtime, gateway=Gateway(raw), llm=llm, clock=lambda: NOW)
    assert marker + ' @U1 作業中。' in llm.prompts[0]
    assert raw['times-a'][0]['text'] == text
