"""Cross-process nonblocking batch lock for macOS/Linux.

Keep the inode in place: unlinking a flock file can let two processes lock
different inodes. The OS releases the actual lock when a process exits.
"""
from datetime import datetime, timedelta, timezone
import fcntl
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)
JST = timezone(timedelta(hours=9))


class LockBusy(RuntimeError):
    """Another batch owns the file lock."""


class BatchLock:
    def __init__(self, base_dir: Path, env: str, *, clock=lambda: datetime.now(JST)):
        if env not in {'test', 'prod'}:
            raise ValueError('env must be test or prod')
        self.path = Path(base_dir).absolute() / env / '.batch.lock'
        self.clock = clock
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self):
        if self.held:
            raise LockBusy('batch lock is already held')
        fd = None
        acquired = False
        try:
            if any(path.is_symlink() for path in (self.path, *self.path.parents)):
                raise RuntimeError('unsafe batch lock path')
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LockBusy('batch is running') from None
            acquired = True
            now = self.clock()
            if now.utcoffset() != timedelta(hours=9):
                raise ValueError('clock must return JST')
            self._warn_stale(fd, now)
            encoded = json.dumps({'pid': os.getpid(), 'createdAt': now.isoformat()}).encode()
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, encoded)
            os.fsync(fd)
            self._fd = fd
            return self
        except BaseException:
            if fd is not None:
                if acquired:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            raise

    @staticmethod
    def _warn_stale(fd, now):
        try:
            raw = os.read(fd, 4096)
            created = datetime.fromisoformat(json.loads(raw)['createdAt'])
            if now - created > timedelta(minutes=30):
                logger.warning('stale batch lock metadata replaced')
        except (ValueError, TypeError, KeyError):
            pass  # Empty/new or malformed metadata never changes OS lock ownership.

    def release(self):
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()
