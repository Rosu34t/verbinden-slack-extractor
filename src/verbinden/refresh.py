"""Asynchronous refresh with per-target cooldown and shared lease."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import logging
import math
from threading import Lock

from .batch import create_gateway, refresh_events, refresh_members
from .batch_lock import BatchLock, LockBusy
from .llm import create_llm_client
from .models import RefreshStatus
from .storage import Storage

JST = timezone(timedelta(hours=9))
logger = logging.getLogger(__name__)
FAILED = '更新に失敗しました。前のデータを表示しています'
MESSAGES = {'idle': None, 'running': '更新中です', 'succeeded': '更新しました', 'failed': FAILED}


def run_refresh(runtime, target, lease, *, clock):
    """Construct job-local adapters; an LLM close failure cannot undo publication."""
    llm = None
    try:
        storage = Storage(runtime.root / runtime.config.paths.data_dir)
        gateway = create_gateway(runtime)
        llm = create_llm_client(runtime)
        function = refresh_events if target == 'events' else refresh_members
        return function(runtime, gateway=gateway, llm=llm, storage=storage, clock=clock, _lease=lease)
    finally:
        if llm is not None:
            try:
                close = getattr(llm, 'close', None)
                if close is not None:
                    close()
            except Exception:
                logger.warning('refresh client cleanup failed: target=%s', target)


class RefreshService:
    def __init__(self, runtime, *, runner=None, clock=lambda: datetime.now(JST), executor=None):
        self.runtime = runtime
        self.clock = clock
        self.runner = runner or (lambda target, lease: run_refresh(runtime, target, lease, clock=clock))
        self.executor = executor if executor is not None else ThreadPoolExecutor(max_workers=1)
        self._lock = Lock()
        idle = RefreshStatus(state='idle', started_at=None, finished_at=None,
                             next_available_at=None, message=None)
        self._statuses = {'events': idle, 'members': idle}

    def close(self):
        self.executor.shutdown(wait=True)

    def _visible(self, target, now):
        status = self._statuses[target]
        if status.next_available_at is not None and status.next_available_at <= now:
            return status.model_copy(update={'next_available_at': None})
        return status

    def statuses(self):
        with self._lock:
            now = self.clock()
            return {target: self._visible(target, now).model_dump(mode='json', by_alias=True)
                    for target in ('events', 'members')}

    @staticmethod
    def _result(code, status=None, error=None, retry_after=None):
        return code, {'success': code == 202, 'data': status.model_dump(mode='json', by_alias=True) if status else None,
                      'error': error}, {'Retry-After': str(retry_after)} if retry_after else {}

    def request(self, target):
        with self._lock:
            now = self.clock()
            status = self._visible(target, now)
            if status.state == 'running':
                return self._result(409, status, '更新中です。しばらく待ってください')
            lease = BatchLock(self.runtime.root / self.runtime.config.paths.data_dir,
                              self.runtime.config.env, clock=self.clock)
            try:
                lease.acquire()
            except LockBusy:
                return self._result(409, status, '更新中です。しばらく待ってください')
            except Exception:
                logger.warning('refresh lock unavailable: target=%s', target)
                return self._result(503, status, FAILED)
            if status.next_available_at is not None:
                lease.release()
                seconds = math.ceil((status.next_available_at - now).total_seconds())
                return self._result(429, status, '前回の更新から時間が経っていません', seconds)
            running = RefreshStatus(state='running', started_at=now, finished_at=None,
                                    next_available_at=now + timedelta(seconds=self.runtime.config.api.refresh_cooldown_seconds),
                                    message=MESSAGES['running'])
            self._statuses = {**self._statuses, target: running}
            try:
                self.executor.submit(self._work, target, lease)
            except Exception:
                lease.release()
                failed = self._finish(target, 'failed', self.clock())
                logger.warning('refresh submission failed: target=%s', target)
                return self._result(503, failed, FAILED)
            return self._result(202, running)

    def _finish(self, target, state, now):
        status = self._statuses[target].model_copy(update={
            'state': state, 'finished_at': now, 'message': MESSAGES[state]})
        self._statuses = {**self._statuses, target: status}
        return status

    def _work(self, target, lease):
        state = 'failed'
        try:
            result = self.runner(target, lease)
            state = 'succeeded'
            logger.info('refresh completed: target=%s count=%s discarded=%s',
                        target, result.count, result.discarded_count)
        except Exception:
            state = 'failed'
            logger.warning('refresh failed: target=%s', target)
        finally:
            lease.release()
            with self._lock:
                self._finish(target, state, self.clock())
