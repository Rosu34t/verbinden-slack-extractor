"""Stored output, live Slack status and asynchronous refresh endpoints."""
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timezone, timedelta
import logging
import os
import re
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware
from slack_sdk import WebClient

from verbinden.config import RuntimeConfig, load_config
from verbinden.models import ApiResponse
from verbinden.present import to_member_response
from verbinden.slack_gateway import LiveStatusService, SlackGateway
from verbinden.storage import Storage
from verbinden.refresh import RefreshService

logger = logging.getLogger(__name__)
JST = timezone(timedelta(hours=9))
DATA_ERROR = 'データがまだ作られていません。バッチを実行してください'


def create_status_service(runtime: RuntimeConfig) -> LiveStatusService:
    client = WebClient(token=runtime.slack_bot_token.get_secret_value())
    gateway = SlackGateway(client, env=runtime.config.env)
    return LiveStatusService(gateway, runtime.config.api.status_cache_seconds)


class RuntimeCors:
    """Read startup configuration after lifespan, before processing HTTP."""

    def __init__(self, app, owner: FastAPI):
        self.app = app
        self.owner = owner

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        runtime = self.owner.state.runtime
        # Browsers preflight a JSON POST, and JS can read Retry-After only when it is exposed.
        middleware = CORSMiddleware(self.app, allow_origins=list(runtime.config.api.cors_origins),
                                    allow_methods=['GET', 'POST'],
                                    allow_headers=['Content-Type'],
                                    expose_headers=['Retry-After'],
                                    allow_credentials=False)
        await middleware(scope, receive, send)


def _bound(value: object) -> tuple[datetime, str]:
    if not isinstance(value, dict):
        raise ValueError('calendar bound must be an object')
    if 'date' in value and 'dateTime' not in value:
        raw = value['date']
        if not isinstance(raw, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
            raise ValueError('invalid calendar date')
        return datetime.combine(date.fromisoformat(raw), time(), tzinfo=JST), 'date'
    if 'dateTime' in value and 'date' not in value:
        raw = value['dateTime']
        if (not isinstance(raw, str) or not re.fullmatch(
                r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?\+09:00', raw)
                or value.get('timeZone') != 'Asia/Tokyo'):
            raise ValueError('invalid calendar datetime')
        return datetime.fromisoformat(raw), 'dateTime'
    raise ValueError('calendar bound must have exactly one time field')


def _event_start(item: object) -> datetime:
    if not isinstance(item, dict):
        raise ValueError('calendar event must be an object')
    for field in ('id', 'summary', 'description'):
        if not isinstance(item.get(field), str):
            raise ValueError('invalid calendar string field')
    if not item['id'].strip() or not item['summary'].strip():
        raise ValueError('empty calendar field')
    if 'location' in item and not isinstance(item['location'], str):
        raise ValueError('invalid calendar location')
    start, start_kind = _bound(item.get('start'))
    end, end_kind = _bound(item.get('end'))
    if start_kind != end_kind or end <= start:
        raise ValueError('invalid calendar interval')
    return start


def _valid_events(values: list[Any]) -> list[dict[str, Any]]:
    validated = []
    skipped = 0
    for item in values:
        try:
            start = _event_start(item)
        except (TypeError, ValueError):
            skipped += 1
            continue
        validated.append((start, item))
    if skipped:
        # Stored content, identifiers and validation errors must stay private.
        logger.warning('api invalid events skipped: count=%s', skipped)
    return [item for _, item in sorted(validated, key=lambda pair: pair[0])]


def _response(data: object) -> dict:
    return ApiResponse(success=True, data=data).model_dump(mode='json')


def _unavailable(endpoint: str) -> JSONResponse:
    # Exception text may contain tokens or stored personal data; never log it.
    logger.warning('api data unavailable: endpoint=%s', endpoint)
    return JSONResponse(status_code=503, content=ApiResponse(
        success=False, error=DATA_ERROR).model_dump(mode='json'))


def create_app(runtime: RuntimeConfig | None = None, *, storage: Storage | None = None,
               status_service: LiveStatusService | None = None,
               refresh_service: RefreshService | None = None) -> FastAPI:
    """Construct safely at import time; load config/secrets only at startup."""
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        try:
            selected = runtime if runtime is not None else load_config(os.getenv('VERBINDEN_ENV', 'test'))
            application.state.runtime = selected
            application.state.storage = storage if storage is not None else Storage(
                selected.root / selected.config.paths.data_dir)
            application.state.status = (status_service if status_service is not None
                                        else create_status_service(selected))
            application.state.refresh = (refresh_service if refresh_service is not None
                                         else RefreshService(selected))
        except Exception:
            logger.error('api startup configuration failed')
            raise RuntimeError('API を起動できません。設定を確認してください') from None
        try:
            yield
        finally:
            application.state.refresh.close()

    application = FastAPI(lifespan=lifespan)
    application.add_middleware(RuntimeCors, owner=application)

    @application.get('/api/health')
    def health():
        return _response({'env': application.state.runtime.config.env})

    @application.get('/api/members')
    def members():
        selected = application.state.runtime
        try:
            profiles = application.state.storage.load_profiles(selected.config.env)
        except Exception:
            return _unavailable('members')
        try:
            statuses = application.state.status.get_statuses([profile.user_id for profile in profiles])
        except Exception:
            logger.warning('api Slack status lookup failed')
            statuses = {}
        return _response([to_member_response(profile, statuses.get(profile.user_id),
                           selected.config.output.members) for profile in profiles])

    @application.get('/api/events')
    def events():
        try:
            values = application.state.storage.load_calendar_events(application.state.runtime.config.env)
            ordered = _valid_events(values)
        except Exception:
            return _unavailable('events')
        return _response(ordered)

    @application.get('/api/refresh/status')
    def refresh_status():
        return _response(application.state.refresh.statuses())

    def begin_refresh(target: str):
        code, payload, headers = application.state.refresh.request(target)
        return JSONResponse(status_code=code, content=payload, headers=headers)

    @application.post('/api/refresh/events')
    def refresh_events():
        return begin_refresh('events')

    @application.post('/api/refresh/members')
    def refresh_members():
        return begin_refresh('members')

    return application


app = create_app()
