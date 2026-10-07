"""Read-only Slack extraction batch with resumable persistence stages."""
from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
from datetime import datetime, timedelta, timezone
import logging

from slack_sdk import WebClient

from .batch_lock import BatchLock
from .calendar_format import to_calendar_event
from .config import RuntimeConfig, load_config
from .llm import LlmClient, create_llm_client
from .models import Post, Profile
from .normalize import normalize_messages, resolve_display_name
from .prompts import build_event_prompt, build_profile_prompt
from .slack_gateway import SlackGateway
from .stats import build_active_hours
from .storage import Storage
from .validate import ProfileFields, validate_events, validate_profile

logger = logging.getLogger(__name__)
JST = timezone(timedelta(hours=9))
_RAW_FIELDS = ('ts', 'user', 'text', 'subtype', 'bot_id', 'thread_ts')


@dataclass(frozen=True)
class BatchResult:
    event_count: int
    profile_count: int
    discarded_event_count: int = 0


@dataclass(frozen=True)
class RefreshResult:
    target: Literal['events', 'members']
    count: int
    discarded_count: int = 0


def create_gateway(runtime: RuntimeConfig) -> SlackGateway:
    return SlackGateway(WebClient(token=runtime.slack_bot_token.get_secret_value()),
                        env=runtime.config.env)


def _parents(messages: list[dict]) -> list[dict]:
    return [message for message in messages
            if not message.get('thread_ts') or message.get('thread_ts') == message.get('ts')]


def _fetch(runtime: RuntimeConfig, gateway: SlackGateway, storage: Storage,
           now: datetime, target: str | None = None) -> dict[str, list[dict]]:
    settings = runtime.config.slack
    channels = tuple(dict.fromkeys((settings.event_channel,
                                   *(item.channel for item in settings.times_channels))))
    if target == 'events':
        channels = (settings.event_channel,)
    elif target == 'members':
        channels = tuple(dict.fromkeys(item.channel for item in settings.times_channels))
    raw = {}
    for name in channels:
        channel = gateway.find_channel(name)
        messages = gateway.fetch_history(channel['id'], now - timedelta(days=settings.period_days))
        # Reactions, files, and other unrelated metadata are never inspected or persisted.
        clean = [{key: message[key] for key in _RAW_FIELDS if key in message}
                 for message in _parents(messages)]
        if target is None:
            storage.save_raw(runtime.config.env, name, clean)
        else:
            storage.save_refresh_raw(runtime.config.env, target, name, clean)
        raw = {**raw, name: clean}
    return raw


def _owner_groups(runtime: RuntimeConfig, gateway: SlackGateway,
                  raw: dict[str, list[dict]]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for item in runtime.config.slack.times_channels:
        channel = gateway.find_channel(item.channel)
        owner = gateway.resolve_owner(channel['id'], item.owner)
        messages = [message for message in _parents(raw[item.channel])
                    if message.get('user') == owner and 'bot_id' not in message]
        grouped = {**grouped, owner: [*grouped.get(owner, []),
                    *[{**message, '_channel': item.channel} for message in messages]]}
    return grouped


def _normalize_owner_message(message: dict, owner: str, name: str) -> list[Post]:
    # Retain the proven owner ID through normalization without adding model fields.
    text = message.get('text', '')
    native_owner = f'<@{owner}>'
    marker = '\ue000OWN_MENTION\ue001'
    if isinstance(text, str):
        while marker in text:
            marker += '\ue002'
        prepared = {**message, 'text': text.replace(native_owner, marker)}
    else:
        prepared = message
    posts = normalize_messages([prepared], message['_channel'], {owner: name})
    return [post.model_copy(update={'text': post.text.replace(marker, native_owner)})
            for post in posts]


def _owner_posts(messages: list[dict], owner: str, name: str) -> list[Post]:
    normalized = [post for message in messages
                  for post in _normalize_owner_message(message, owner, name)]
    unique = {post.id: post for post in normalized}
    return sorted(unique.values(), key=lambda post: post.posted_at)


def _profile(owner: str, messages: list[dict], gateway: SlackGateway,
             llm: LlmClient, now: datetime, *, strict: bool = False) -> Profile:
    user = gateway.fetch_user(owner)
    name = resolve_display_name(user)
    posts = [] if user.get('is_bot') else _owner_posts(messages, owner, name)
    fields = ProfileFields()
    if posts:
        try:
            response = llm.generate_json(build_profile_prompt(name, posts, name))
            fields, reasons = validate_profile(response)
            if reasons:
                if strict:
                    logger.warning('members validation discarded=%d', len(reasons))
                else:
                    logger.warning('プロフィール検証: user_id=%s 不正項目=%d', owner, len(reasons))
        except Exception:
            if strict:
                raise
            # Neither provider exceptions nor posted text are safe log arguments.
            logger.warning('プロフィール生成に失敗: user_id=%s', owner)
    return Profile(id='mem-' + owner, user_id=owner, display_name=name,
                   icon_url=(user.get('profile') or {}).get('image_192') or None,
                   active_hours=build_active_hours(posts), post_count=len(posts),
                   updated_at=now.date(), **fields.model_dump())


def _extract_events(runtime: RuntimeConfig, gateway: SlackGateway, llm: LlmClient,
             raw: dict[str, list[dict]], now: datetime):
    channel = runtime.config.slack.event_channel
    messages = _parents(raw[channel])
    authors = tuple(dict.fromkeys(message.get('user') for message in messages
                                 if message.get('user') and 'bot_id' not in message))
    users = {author: gateway.fetch_user(author) for author in authors}
    names = {author: resolve_display_name(user) for author, user in users.items()}
    humans = [message for message in messages
              if not users.get(message.get('user'), {}).get('is_bot')]
    posts = normalize_messages(humans, channel, names)
    events, reasons = (validate_events(llm.generate_json(build_event_prompt(posts, now.date())), posts)
                       if posts else ([], []))
    return events, len(reasons)


def _extract(runtime: RuntimeConfig, gateway: SlackGateway, llm: LlmClient,
             raw: dict[str, list[dict]], now: datetime):
    events, discarded = _extract_events(runtime, gateway, llm, raw, now)
    profiles = [_profile(owner, messages, gateway, llm, now)
                for owner, messages in _owner_groups(runtime, gateway, raw).items()]
    return events, profiles, discarded


def _calendar(events):
    return [to_calendar_event(event) for event in
            sorted(events, key=lambda event: (event.date, event.source_post_id))]


def _refresh(target, runtime, *, gateway, llm, storage, clock, _lease=None) -> RefreshResult:
    now = clock()
    if now.utcoffset() != timedelta(hours=9):
        raise ValueError('clock must return a JST datetime')
    env = runtime.config.env
    owned = _lease is None
    lease = _lease if _lease is not None else BatchLock(storage.base_dir, env, clock=lambda: now)
    expected = storage.base_dir / env / '.batch.lock'
    if not owned and (not lease.held or lease.path != expected):
        raise ValueError('refresh requires a held lease for its storage environment')
    if owned:
        lease.acquire()
    try:
        raw_storage = Storage(storage.base_dir, clock=lambda: now)
        raw = _fetch(runtime, gateway, raw_storage, now, target)
        if target == 'events':
            events, discarded = _extract_events(runtime, gateway, llm, raw, now)
            storage.save_refresh_events(env, events, _calendar(events))
            result = RefreshResult(target, len(events), discarded)
        else:
            profiles = [_profile(owner, messages, gateway, llm, now, strict=True)
                        for owner, messages in _owner_groups(runtime, gateway, raw).items()]
            storage.save_refresh_members(env, profiles)
            result = RefreshResult(target, len(profiles))
        logger.info('refresh target=%s count=%d discarded=%d',
                    result.target, result.count, result.discarded_count)
        return result
    finally:
        if owned:
            lease.release()


def refresh_events(runtime, *, gateway, llm, storage, clock, _lease=None) -> RefreshResult:
    return _refresh('events', runtime, gateway=gateway, llm=llm, storage=storage,
                    clock=clock, _lease=_lease)


def refresh_members(runtime, *, gateway, llm, storage, clock, _lease=None) -> RefreshResult:
    return _refresh('members', runtime, gateway=gateway, llm=llm, storage=storage,
                    clock=clock, _lease=_lease)


def run_batch(runtime: RuntimeConfig, from_stage: str = 'fetch', *,
              gateway: SlackGateway | None = None, llm: LlmClient | None = None,
              storage: Storage | None = None,
              clock: Callable[[], datetime] = lambda: datetime.now(JST)) -> BatchResult:
    """Run from the requested stage; injected services are owned by the caller."""
    if from_stage not in {'fetch', 'extract', 'output'}:
        raise ValueError('from_stage must be fetch, extract, or output')
    now = clock()
    if now.utcoffset() != timedelta(hours=9):
        raise ValueError('clock must return a JST datetime')
    storage = storage if storage is not None else Storage(
        runtime.root / runtime.config.paths.data_dir, clock=lambda: now)
    env = runtime.config.env
    owned_llm = None
    lease = BatchLock(storage.base_dir, env, clock=lambda: now).acquire()
    try:
        discarded = 0
        if from_stage == 'output':
            events, profiles = storage.load_extracted(env)
        else:
            gateway = gateway if gateway is not None else create_gateway(runtime)
            raw = (_fetch(runtime, gateway, storage, now) if from_stage == 'fetch'
                   else storage.load_latest_raw(env))
            if llm is None:
                owned_llm = create_llm_client(runtime)
                llm = owned_llm
            events, profiles, discarded = _extract(runtime, gateway, llm, raw, now)
            storage.save_extracted(env, events, profiles)
        calendar = _calendar(events)
        storage.save_out(env, profiles, calendar)
        logger.info('イベント %d件（捨てた %d件）、プロフィール %d件',
                    len(events), discarded, len(profiles))
        return BatchResult(len(events), len(profiles), discarded)
    finally:
        lease.release()
        if owned_llm is not None:
            close = getattr(owned_llm, 'close', None)
            if close is not None:
                close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Slack読み取り抽出バッチ')
    parser.add_argument('--env', choices=('test', 'prod'), default='test')
    parser.add_argument('--from-stage', choices=('fetch', 'extract', 'output'), default='fetch')
    args = parser.parse_args(argv)
    try:
        run_batch(load_config(args.env), from_stage=args.from_stage)
    except Exception:
        logger.error('バッチに失敗しました。設定・接続・保存データを確認してください。')
        return 1
    return 0


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    raise SystemExit(main())
