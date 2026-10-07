"""Read-only Slack access and short-lived, per-user status caching."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

import emoji
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

from verbinden.models import LiveStatus


class SlackGatewayError(RuntimeError):
    """Safe operator-facing error; never embeds a Slack response or token."""


class SlackGateway:
    def __init__(self, client: WebClient, env: str = 'test'):
        if env not in {'test', 'prod'}:
            raise ValueError('env: test または prod を指定してください')
        self._client = client
        self._env = env
        self._channel_names: dict[str, str] = {}
        # WebClient DEBUG logs contain tokens and complete message bodies.
        logger = logging.Logger('verbinden.slack.safe', level=logging.CRITICAL)
        logger.disabled = True
        self._client._logger = logger
        if not any(isinstance(h, RateLimitErrorRetryHandler) for h in client.retry_handlers):
            client.retry_handlers = [*client.retry_handlers, RateLimitErrorRetryHandler(max_retry_count=2)]

    def _call(self, method: str, context: str, **kwargs: Any) -> Mapping[str, Any]:
        try:
            return getattr(self._client, method)(**kwargs)
        except SlackApiError as exc:
            code = exc.response.get('error', '')
            messages = {
                'channel_not_found': f'チャンネル #{context} が見つかりません（config を確認）',
                'not_in_channel': f'Bot が #{context} に参加していません。チャンネルで /invite してください',
                'invalid_auth': f'Slack トークンが無効です（.env.{self._env} を確認）',
            }
            raise SlackGatewayError(messages.get(code, 'Slack の読み取りに失敗しました')) from None
        except Exception:
            raise SlackGatewayError('Slack の読み取りに失敗しました') from None

    def _pages(self, method: str, context: str, **kwargs: Any):
        cursor = ''
        visited = set()
        while True:
            response = self._call(method, context, **kwargs, cursor=cursor)
            yield response
            cursor = response.get('response_metadata', {}).get('next_cursor', '')
            if not cursor:
                return
            if not isinstance(cursor, str) or cursor in visited:
                raise SlackGatewayError('Slack のページ送りが完了できませんでした')
            visited.add(cursor)

    def find_channel(self, name: str) -> dict:
        if not isinstance(name, str) or not name or name.startswith('#'):
            raise ValueError('name: # なしのチャンネル名を指定してください')
        for page in self._pages('conversations_list', name, types='public_channel', exclude_archived=True, limit=200):
            for channel in page.get('channels', []):
                if channel.get('name') == name:
                    self._channel_names = {**self._channel_names, channel['id']: name}
                    return dict(channel)
        raise SlackGatewayError(f'チャンネル #{name} が見つかりません（config を確認）')

    def fetch_history(self, channel_id: str, oldest: datetime) -> list[dict]:
        self._require_id(channel_id, 'channel_id')
        if oldest.tzinfo is None or oldest.utcoffset() is None:
            raise ValueError('oldest: timezone を指定してください')
        context = self._channel_names.get(channel_id, channel_id)
        pages = self._pages('conversations_history', context, channel=channel_id,
                            oldest=str(oldest.timestamp()), limit=100)
        # Only channel parents are fetched, including parents that have replies.
        return [dict(message) for page in pages for message in page.get('messages', [])
                if not message.get('thread_ts') or message.get('thread_ts') == message.get('ts')]

    @staticmethod
    def _require_id(value: str, field: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'{field}: 空でない文字列を指定してください')

    def fetch_user(self, user_id: str) -> dict:
        self._require_id(user_id, 'user_id')
        return dict(self._call('users_info', user_id, user=user_id)['user'])

    def fetch_presence(self, user_id: str) -> str:
        self._require_id(user_id, 'user_id')
        presence = self._call('users_getPresence', user_id, user=user_id)['presence']
        if presence not in {'active', 'away'}:
            raise SlackGatewayError('Slack の在席情報を確認できませんでした')
        return presence

    def resolve_owner(self, channel_id: str, override: str | None = None) -> str:
        self._require_id(channel_id, 'channel_id')
        if override is not None:
            self._require_id(override, 'owner')
            return override
        context = self._channel_names.get(channel_id, channel_id)
        owner = self._call('conversations_info', context, channel=channel_id).get('channel', {}).get('creator')
        if not isinstance(owner, str) or not owner:
            raise SlackGatewayError('チャンネルの作成者を確認できませんでした（config の owner を指定）')
        return owner


class LiveStatusService:
    def __init__(self, gateway: SlackGateway, cache_seconds: int,
                 clock: Callable[[], float] = time.time):
        if isinstance(cache_seconds, bool) or not isinstance(cache_seconds, int) or cache_seconds < 0:
            raise ValueError('cache_seconds: 0 以上の整数を指定してください')
        self._gateway = gateway
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._cache: dict[str, tuple[float, LiveStatus | None, float | None]] = {}

    def get_statuses(self, user_ids: list[str]) -> dict[str, LiveStatus | None]:
        now = self._clock()
        result = {}
        for user_id in dict.fromkeys(user_ids):
            cached = self._cache.get(user_id)
            if cached is not None and now < cached[0]:
                status = cached[1]
                if status is not None and cached[2] is not None and cached[2] <= now:
                    status = status.model_copy(update={'text': None, 'emoji': None})
                result[user_id] = status
                continue
            try:
                status, expiration = self._fetch_status(user_id, now)
            except Exception:
                status, expiration = None, None
            result[user_id] = status
            self._cache = {**self._cache, user_id: (now + self._cache_seconds, status, expiration)}
        return result

    def _fetch_status(self, user_id: str, now: float) -> tuple[LiveStatus, float | None]:
        profile = self._gateway.fetch_user(user_id).get('profile', {})
        presence = self._gateway.fetch_presence(user_id)
        expiration = profile.get('status_expiration')
        expired = expiration not in (None, 0) and expiration <= now
        text = None if expired else profile.get('status_text') or None
        symbol = None if expired else profile.get('status_emoji') or None
        return LiveStatus(text=text, emoji=emoji.emojize(symbol, language='alias') if symbol else None,
                          presence=presence), expiration or None
