from copy import deepcopy

import pytest
from pydantic import ValidationError

from verbinden.config import AppConfig, ConfigError, load_config


@pytest.fixture
def config_data():
    return {
        "env": "test",
        "slack": {"eventChannel": "general", "timesChannels": [{"channel": "times-test1", "owner": None}], "periodDays": 30},
        "llm": {"provider": "fake", "model": "fixture", "maxRetries": 3, "timeoutSeconds": 60},
        "api": {"statusCacheSeconds": 60, "corsOrigins": ["http://localhost:3000"]},
        "output": {"members": {"id": "id", "name": "name", "iconUrl": "iconUrl", "status": "status", "intro": "intro"}},
        "paths": {"dataDir": "data"},
    }


@pytest.mark.parametrize("section,key,value", [
    ("slack", "periodDays", 0), ("slack", "periodDays", -1),
    ("llm", "provider", "unknown"), ("llm", "maxRetries", -1),
    ("llm", "timeoutSeconds", 0), ("api", "statusCacheSeconds", -1),
    ("slack", "eventChannel", " "),
])
def test_rejects_invalid_configuration(config_data, section, key, value):
    data = deepcopy(config_data)
    data[section][key] = value
    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


def test_configuration_accepts_camel_case_and_is_frozen(config_data):
    config = AppConfig.model_validate(config_data)
    assert config.slack.period_days == 30
    expected = deepcopy(config_data)
    expected["api"]["refreshCooldownSeconds"] = 600
    assert config.model_dump(mode="json", by_alias=True) == expected
    with pytest.raises(ValidationError):
        config.env = "prod"


@pytest.mark.parametrize("field_map", [{"unknown": "name"}, {"id": ""}, {"id": "same", "name": "same"}])
def test_rejects_unusable_member_field_maps(config_data, field_map):
    config_data["output"]["members"] = field_map
    with pytest.raises(ValidationError):
        AppConfig.model_validate(config_data)


def write_config(tmp_path, config_data):
    import yaml
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.test.yaml").write_text(yaml.safe_dump(config_data), encoding="utf-8")


def test_loads_dotenv_without_mutating_process_environment(tmp_path, config_data, monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    write_config(tmp_path, config_data)
    (tmp_path / ".env.test").write_text("SLACK_BOT_TOKEN=fictional-test-token\n", encoding="utf-8")
    result = load_config("test", root=tmp_path)
    assert result.config.env == "test"
    assert result.slack_bot_token.get_secret_value() == "fictional-test-token"
    assert "fictional-test-token" not in repr(result)
    import os
    assert "SLACK_BOT_TOKEN" not in os.environ


@pytest.mark.parametrize("provider", ["fake", "ollama"])
def test_non_gemini_provider_does_not_require_gemini_key(tmp_path, config_data, monkeypatch, provider):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "fictional-env-token")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    config_data["llm"]["provider"] = provider
    write_config(tmp_path, config_data)
    assert load_config("test", root=tmp_path).gemini_api_key is None


def test_gemini_requires_api_key(tmp_path, config_data, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "fictional-env-token")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    config_data["llm"]["provider"] = "gemini"
    write_config(tmp_path, config_data)
    with pytest.raises(ConfigError, match="GEMINI_API_KEY.*.env.test"):
        load_config("test", root=tmp_path)


def test_missing_slack_token_is_actionable(tmp_path, config_data, monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    write_config(tmp_path, config_data)
    with pytest.raises(ConfigError, match="SLACK_BOT_TOKEN.*.env.test"):
        load_config("test", root=tmp_path)


@pytest.mark.parametrize("env", ["../prod", "staging"])
def test_rejects_unknown_environment_before_file_access(tmp_path, env):
    with pytest.raises(ConfigError, match="環境"):
        load_config(env, root=tmp_path)


def test_missing_configuration_file_is_actionable(tmp_path):
    with pytest.raises(ConfigError, match="config.test.yaml"):
        load_config("test", root=tmp_path)


@pytest.mark.parametrize("contents", ["{broken", "[]", "env: prod"])
def test_invalid_yaml_or_configuration_is_wrapped(tmp_path, contents):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.test.yaml").write_text(contents, encoding="utf-8")
    with pytest.raises(ConfigError, match="設定"):
        load_config("test", root=tmp_path)


def test_member_mapping_cannot_change_after_validation(config_data):
    config = AppConfig.model_validate(config_data)
    with pytest.raises(TypeError):
        config.output.members["id"] = "changed"
    config_data["output"]["members"]["id"] = "changed"
    assert config.output.members["id"] == "id"


def test_dotenv_credentials_override_process(tmp_path, config_data, monkeypatch):
    write_config(tmp_path, config_data)
    (tmp_path / ".env.test").write_text("SLACK_BOT_TOKEN=fictional-file-token\n", encoding="utf-8")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "fictional-process-token")
    assert load_config("test", root=tmp_path).slack_bot_token.get_secret_value() == "fictional-file-token"


def test_env_mismatch_is_rejected(tmp_path, config_data):
    config_data["env"] = "prod"
    write_config(tmp_path, config_data)
    with pytest.raises(ConfigError, match="一致"):
        load_config("test", root=tmp_path)


@pytest.mark.parametrize("env", ["test", "prod"])
def test_shipped_configuration_examples_are_valid(env):
    from pathlib import Path
    import yaml
    project_root = Path(__file__).resolve().parents[1]
    contents = (project_root / "config" / f"config.{env}.yaml").read_text(encoding="utf-8")
    assert AppConfig.model_validate(yaml.safe_load(contents)).env == env


def test_default_root_does_not_depend_on_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "fictional-token")
    monkeypatch.setenv("GEMINI_API_KEY", "fictional-key")
    result = load_config("test")
    assert result.config.env == "test"
    assert result.root.name == "slack_extractor"


@pytest.mark.parametrize("file_value", ["", "   "])
def test_empty_dotenv_value_falls_back_to_shell(tmp_path, config_data, monkeypatch, file_value):
    write_config(tmp_path, config_data)
    (tmp_path / ".env.test").write_text(f'SLACK_BOT_TOKEN="{file_value}"\n', encoding="utf-8")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "fictional-shell-token")
    assert load_config(root=tmp_path).slack_bot_token.get_secret_value() == "fictional-shell-token"


@pytest.mark.parametrize("shell_value,expected_warning", [
    ("fictional-production-token", True), ("fictional-test-token", False), ("", False), ("   ", False),
])
def test_credential_conflict_logs_key_without_values(tmp_path, config_data, monkeypatch, caplog, shell_value, expected_warning):
    write_config(tmp_path, config_data)
    (tmp_path / ".env.test").write_text("SLACK_BOT_TOKEN=fictional-test-token\n", encoding="utf-8")
    monkeypatch.setenv("SLACK_BOT_TOKEN", shell_value)
    result = load_config(root=tmp_path)
    assert result.slack_bot_token.get_secret_value() == "fictional-test-token"
    assert bool(caplog.records) is expected_warning
    if expected_warning:
        assert "SLACK_BOT_TOKEN" in caplog.text
        assert ".env.test" in caplog.text
        assert all(record.levelname == "WARNING" for record in caplog.records)
    assert "fictional-test-token" not in caplog.text
    assert "fictional-production-token" not in caplog.text


def test_config_validation_reports_all_locations_and_reasons_without_values(tmp_path, config_data):
    data = deepcopy(config_data)
    data["slack"]["periodDays"] = 0
    data["llm"]["provider"] = "fictional-sensitive-value"
    write_config(tmp_path, data)
    with pytest.raises(ConfigError) as error:
        load_config(root=tmp_path)
    message = str(error.value)
    assert "config.test.yaml" in message
    assert "slack.periodDays:" in message
    assert "greater than 0" in message
    assert "llm.provider:" in message
    assert "gemini" in message
    assert "fictional-sensitive-value" not in message


def test_missing_file_and_invalid_yaml_have_distinct_messages(tmp_path):
    with pytest.raises(ConfigError, match="見つかりません") as missing:
        load_config(root=tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.test.yaml").write_text("{broken", encoding="utf-8")
    with pytest.raises(ConfigError, match="YAML") as invalid:
        load_config(root=tmp_path)
    assert str(missing.value) != str(invalid.value)


@pytest.mark.parametrize("key,provider,attribute", [
    ("GEMINI_API_KEY", "gemini", "gemini_api_key"),
    ("OLLAMA_BASE_URL", "ollama", "ollama_base_url"),
])
def test_other_provider_settings_follow_dotenv_precedence(tmp_path, config_data, monkeypatch, key, provider, attribute):
    data = deepcopy(config_data)
    data["llm"]["provider"] = provider
    write_config(tmp_path, data)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "fictional-slack-token")
    monkeypatch.setenv(key, "fictional-shell-value")
    (tmp_path / ".env.test").write_text(f"{key}=fictional-file-value\n", encoding="utf-8")
    value = getattr(load_config(root=tmp_path), attribute)
    assert (value.get_secret_value() if key == "GEMINI_API_KEY" else value) == "fictional-file-value"


@pytest.mark.parametrize("value", [0, 600, 1200])
def test_refresh_cooldown_accepts_nonnegative_values(config_data, value):
    config_data["api"]["refreshCooldownSeconds"] = value
    assert AppConfig.model_validate(config_data).api.refresh_cooldown_seconds == value


def test_refresh_cooldown_defaults_for_existing_configuration(config_data):
    assert AppConfig.model_validate(config_data).api.refresh_cooldown_seconds == 600


def test_refresh_cooldown_rejects_negative_value(config_data):
    config_data["api"]["refreshCooldownSeconds"] = -1
    with pytest.raises(ValidationError):
        AppConfig.model_validate(config_data)


@pytest.mark.parametrize("value", [None, "", "   ", "x", "fictional-legacy-token" * 4])
def test_legacy_refresh_token_is_ignored(tmp_path, config_data, monkeypatch, value, caplog):
    write_config(tmp_path, config_data)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "fictional-slack-token")
    monkeypatch.setenv("REFRESH_TOKEN", "fictional-shell-legacy")
    if value is not None:
        (tmp_path / ".env.test").write_text(f'REFRESH_TOKEN="{value}"\n', encoding="utf-8")
    result = load_config(root=tmp_path)
    assert not hasattr(result, "refresh_token")
    assert "refreshToken" not in result.model_dump()
    assert "REFRESH_TOKEN" not in caplog.text
    assert "fictional-shell-legacy" not in caplog.text
    if value and value.strip():
        assert value not in result.model_dump().values()
        assert value not in caplog.text
