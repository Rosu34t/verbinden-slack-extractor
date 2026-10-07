"""Validate configuration and load credentials without changing process state."""
from collections.abc import Mapping
from types import MappingProxyType
from pathlib import Path
from typing import Annotated, Literal
import os
import logging

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, ValidationError, field_serializer, field_validator
from pydantic.alias_generators import to_camel
import yaml

logger = logging.getLogger(__name__)

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ConfigModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", alias_generator=to_camel, populate_by_name=True)


class TimesChannel(ConfigModel):
    channel: Name
    owner: Name | None = None


class SlackConfig(ConfigModel):
    event_channel: Name
    times_channels: tuple[TimesChannel, ...]
    period_days: int = Field(gt=0)


class LlmConfig(ConfigModel):
    provider: Literal["gemini", "ollama", "fake"]
    model: Name
    max_retries: int = Field(ge=0)
    timeout_seconds: int = Field(gt=0)


class ApiConfig(ConfigModel):
    status_cache_seconds: int = Field(ge=0)
    cors_origins: tuple[Name, ...]
    refresh_cooldown_seconds: int = Field(default=600, ge=0)


class OutputConfig(ConfigModel):
    members: Mapping[str, Name]

    @field_validator("members")
    @classmethod
    def validate_members(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        allowed = {"id", "name", "iconUrl", "status", "intro"}
        if not set(value).issubset(allowed):
            raise ValueError("未知のメンバー出力項目があります")
        if len(set(value.values())) != len(value):
            raise ValueError("出力キー名が重複しています")
        return MappingProxyType(dict(value))

    @field_serializer("members")
    def serialize_members(self, value: Mapping[str, str]) -> dict[str, str]:
        return dict(value)


class PathsConfig(ConfigModel):
    data_dir: Name


class AppConfig(ConfigModel):
    env: Literal["test", "prod"]
    slack: SlackConfig
    llm: LlmConfig
    api: ApiConfig
    output: OutputConfig
    paths: PathsConfig


class RuntimeConfig(ConfigModel):
    config: AppConfig
    root: Path
    slack_bot_token: SecretStr
    gemini_api_key: SecretStr | None = None
    ollama_base_url: str = "http://localhost:11434"


class ConfigError(ValueError):
    """An actionable startup configuration error with no secret values."""


def _read_config(path: Path, env: str) -> AppConfig:
    try:
        with path.open(encoding="utf-8") as stream:
            config = AppConfig.model_validate(yaml.safe_load(stream))
    except FileNotFoundError:
        raise ConfigError(f"設定ファイルが見つかりません（{path.name}）") from None
    except OSError:
        raise ConfigError(f"設定ファイルを読み込めません（{path.name}）。アクセス権を確認してください") from None
    except yaml.YAMLError:
        raise ConfigError(f"設定ファイルを YAML として読めません（{path.name}）") from None
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or '(root)'}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        raise ConfigError(f"設定ファイル {path.name} の値が不正です: {details}") from None
    if config.env != env:
        raise ConfigError(f"設定の env が実行環境と一致しません（{path.name}）")
    return config


def load_config(env: str = "test", *, root: Path | None = None) -> RuntimeConfig:
    """Nonblank .env values override process variables without changing them."""
    if env not in {"test", "prod"}:
        raise ConfigError("環境は test または prod を指定してください")
    project_root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    config = _read_config(project_root / "config" / f"config.{env}.yaml", env)
    env_file = project_root / f".env.{env}"
    try:
        file_values = dotenv_values(env_file, interpolate=False)
    except OSError:
        raise ConfigError(f"設定ファイルを読み込めません（{env_file.name}）") from None

    overrides = {
        key: value for key, value in file_values.items()
        if key != "REFRESH_TOKEN" and value is not None and value.strip()
    }
    for key, value in overrides.items():
        shell_value = os.environ.get(key)
        if shell_value and shell_value.strip() and shell_value != value:
            logger.warning("%s がシェルの値と異なります。%s の値を優先します", key, env_file.name)
    values = {**os.environ, **overrides}

    def required_secret(key: str) -> SecretStr:
        value = values.get(key)
        if not value or not value.strip():
            raise ConfigError(f"{key} が設定されていません（{env_file.name}）")
        return SecretStr(value.strip())

    slack_token = required_secret("SLACK_BOT_TOKEN")
    gemini_key = required_secret("GEMINI_API_KEY") if config.llm.provider == "gemini" else None
    return RuntimeConfig(
        config=config, root=project_root, slack_bot_token=slack_token,
        gemini_api_key=gemini_key,
        ollama_base_url=values.get("OLLAMA_BASE_URL") or "http://localhost:11434",
    )
