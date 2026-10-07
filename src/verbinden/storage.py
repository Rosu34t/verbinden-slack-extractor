"""UTF-8 JSON persistence with atomic replacement of individual files."""

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from pydantic import ValidationError

from .models import Event, Profile


logger = logging.getLogger(__name__)


class StorageError(RuntimeError):
    """A persistence failure whose message never includes stored payloads."""


class Storage:
    """Use one instance per fetch run; a timestamp collision fails safely.

    Each file is replaced atomically. The events/profiles pair is not a
    transaction: readers should consume it only after the batch completes.
    """

    def __init__(self, base_dir: Path, clock: Callable[[], datetime] | None = None):
        self.base_dir = Path(base_dir).absolute()
        self._clock = clock or (lambda: datetime.now(timezone(timedelta(hours=9))))
        self._raw_runs: dict[str, Path] = {}
        self._refresh_runs: dict[tuple[str, str], Path] = {}

    def _path(self, stage: str, env: str, *parts: str) -> Path:
        if env not in {"test", "prod"}:
            raise StorageError("env must be test or prod")
        target = self.base_dir.joinpath(stage, env, *parts)
        for path in (target, *target.parents):
            if path.is_symlink():
                raise StorageError("symbolic links are not permitted in storage paths")
        return target

    @staticmethod
    def _channel(channel: str) -> None:
        if (not isinstance(channel, str) or not channel.strip() or channel in {".", ".."}
                or any(char in channel for char in "#/\\")
                or any(ord(char) < 32 or ord(char) == 127 for char in channel)):
            raise StorageError("channel must be a channel name without # or path separators")

    @staticmethod
    def _objects(value: object) -> list[dict[str, Any]]:
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise StorageError("stored data must be an array of objects")
        return value

    @staticmethod
    def _json_models(values: Sequence[Event] | Sequence[Profile]) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json", by_alias=True) for item in values]

    def _write(self, path: Path, value: object) -> Path:
        temporary: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".tmp-", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            return path
        except (OSError, TypeError, ValueError):
            raise StorageError("could not save JSON data") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    raise StorageError("could not clean temporary JSON file") from None

    @staticmethod
    def _read(path: Path) -> object:
        try:
            with path.open(encoding="utf-8") as handle:
                def reject_constant(value: str):
                    raise ValueError("nonstandard JSON number")

                return json.load(handle, parse_constant=reject_constant)
        except (OSError, UnicodeError, ValueError):
            raise StorageError("could not read valid JSON data") from None

    def save_raw(self, env: str, channel: str, messages: list[dict[str, Any]]) -> Path:
        self._channel(channel)
        self._objects(messages)
        self._path("raw", env)
        if env not in self._raw_runs:
            timestamp = self._clock().strftime("%Y%m%d-%H%M%S")
            run = self._path("raw", env, timestamp)
            try:
                run.mkdir(parents=True, exist_ok=False)
            except OSError:
                raise StorageError("could not create raw run; timestamp may already exist") from None
            self._raw_runs = {**self._raw_runs, env: run}
        run = self._raw_runs[env]
        path = self._path("raw", env, run.name, f"{channel}.json")
        return self._write(path, messages)

    def save_refresh_raw(self, env: str, target: str, channel: str,
                         messages: list[dict[str, Any]]) -> Path:
        if target not in {"events", "members"}:
            raise StorageError("invalid refresh target")
        self._channel(channel)
        self._objects(messages)
        key = (env, target)
        if key not in self._refresh_runs:
            timestamp = self._clock().strftime("%Y%m%d-%H%M%S")
            run = self._path("raw", env, f"{timestamp}-{target}")
            try:
                run.mkdir(parents=True, exist_ok=False)
            except OSError:
                raise StorageError("could not create refresh raw run") from None
            self._refresh_runs = {**self._refresh_runs, key: run}
        run = self._refresh_runs[key]
        return self._write(self._path("raw", env, run.name, f"{channel}.json"), messages)

    @staticmethod
    def _prepare_bytes(path: Path, value: bytes) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                                             prefix=".tmp-", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            return temporary
        except BaseException:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise

    def _publish_refresh(self, env: str, filename: str, extracted: object, output: object) -> None:
        """Prepare both files first; roll back extraction if output replacement fails."""
        first = self._path("extracted", env, filename)
        second = self._path("out", env, filename)
        temporary: list[Path] = []
        committed = False
        backup = None
        try:
            # Serialize before touching any persisted data, rejecting non-JSON numbers.
            values = [json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode() + b"\n"
                      for value in (extracted, output)]
            for path, value in zip((first, second), values):
                temporary.append(self._prepare_bytes(path, value))
            if first.exists():
                backup = self._prepare_bytes(first, first.read_bytes())
                temporary.append(backup)
            os.replace(temporary[0], first)
            committed = True
            os.replace(temporary[1], second)
        except (OSError, TypeError, ValueError):
            if committed:
                try:
                    if backup is not None:
                        os.replace(backup, first)
                    else:
                        first.unlink(missing_ok=True)
                except OSError:
                    raise StorageError("could not publish or restore refresh data") from None
            raise StorageError("could not publish refresh data") from None
        finally:
            failed_cleanup = 0
            for path in temporary:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    failed_cleanup += 1
            if failed_cleanup:
                logger.warning("refresh temporary cleanup count=%d", failed_cleanup)

    def save_refresh_events(self, env: str, events: Sequence[Event],
                            calendar_events: list[dict[str, Any]]) -> None:
        self._objects(calendar_events)
        self._publish_refresh(env, "events.json", self._json_models(events), calendar_events)

    def save_refresh_members(self, env: str, profiles: Sequence[Profile]) -> None:
        values = self._json_models(profiles)
        self._publish_refresh(env, "profiles.json", values, values)

    def load_latest_raw(self, env: str) -> dict[str, list[dict[str, Any]]]:
        root = self._path("raw", env)
        try:
            runs = sorted(path.name for path in root.iterdir()
                          if re.fullmatch(r"\d{8}-\d{6}", path.name) and path.is_dir())
            if not runs:
                raise StorageError("no raw run found")
            run = self._path("raw", env, runs[-1])
            files = sorted(run.glob("*.json"))
            if not files:
                raise StorageError("raw run contains no channel files")
            result = {}
            for file in files:
                self._channel(file.stem)
                path = self._path("raw", env, run.name, file.name)
                result = {**result, file.stem: self._objects(self._read(path))}
            return result
        except OSError:
            raise StorageError("could not list raw runs") from None

    def save_extracted(self, env: str, events: Sequence[Event], profiles: Sequence[Profile]) -> None:
        self._write(self._path("extracted", env, "events.json"), self._json_models(events))
        self._write(self._path("extracted", env, "profiles.json"), self._json_models(profiles))

    def load_extracted(self, env: str) -> tuple[list[Event], list[Profile]]:
        try:
            events = self._objects(self._read(self._path("extracted", env, "events.json")))
            profiles = self._objects(self._read(self._path("extracted", env, "profiles.json")))
            return ([Event.model_validate(item) for item in events],
                    [Profile.model_validate(item) for item in profiles])
        except ValidationError:
            raise StorageError("stored extraction does not match the data models") from None

    def save_out(self, env: str, members_base: Sequence[Profile],
                 calendar_events: list[dict[str, Any]]) -> None:
        self._objects(calendar_events)
        self._write(self._path("out", env, "profiles.json"), self._json_models(members_base))
        self._write(self._path("out", env, "events.json"), calendar_events)

    def load_profiles(self, env: str) -> list[Profile]:
        """Read member data independently of calendar data."""
        try:
            profiles = self._objects(self._read(self._path("out", env, "profiles.json")))
            return [Profile.model_validate(item) for item in profiles]
        except ValidationError:
            raise StorageError("stored profiles do not match the data model") from None

    def load_calendar_events(self, env: str) -> list[Any]:
        """Read an array; the API validates and skips individual invalid entries."""
        values = self._read(self._path("out", env, "events.json"))
        if not isinstance(values, list):
            raise StorageError("stored calendar data must be an array")
        return values

    def load_out(self, env: str) -> tuple[list[Profile], list[dict[str, Any]]]:
        return self.load_profiles(env), self._objects(self.load_calendar_events(env))
