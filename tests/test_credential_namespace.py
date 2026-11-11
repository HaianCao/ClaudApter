import os

import pytest

from claudapter.config import Config, Provider, secret_names, secret_var, validate
from claudapter.credentials import get_token, remove_token, store_token
from claudapter.errors import ClaudapterError
from claudapter.launcher import build_invocation


@pytest.mark.parametrize(
    "provider_id", ["anthropic_auth", "claude_code_oauth", "claude_code_setup", "claude-code-oauth"]
)
def test_native_auth_is_never_selected_and_gateway_token_survives(tmp_path, monkeypatch, provider_id):
    name, legacy = secret_names(provider_id)
    monkeypatch.setenv("CLAUDE_CA_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv(legacy, "synthetic-native-token")
    monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("claudapter.launcher.shutil.which", lambda _: "claude.exe")
    provider = Provider(provider_id, "Fixture", "https://example.test/v1", execution_mode="gateway", protocol="chat")
    config = Config({provider_id: provider}, path=tmp_path / "config.toml")
    validate(config)
    assert name != legacy
    assert get_token(config, provider_id) == ""
    store_token(config, provider_id, "synthetic-adapter-key")
    content = (tmp_path / ".env").read_text()
    assert f'{name}="synthetic-adapter-key"' in content
    assert "synthetic-native-token" not in content
    monkeypatch.setenv(name, "synthetic-environment-adapter-key")
    args, env = build_invocation(
        config,
        provider,
        "m",
        "default",
        [],
        gateway_url="http://127.0.0.1:12345",
        gateway_token="synthetic-local-token",
    )
    assert env["ANTHROPIC_AUTH_TOKEN"] == "synthetic-local-token"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "CLAUDE_CODE_SETUP_TOKEN" not in env
    assert name not in env and "synthetic-adapter-key" not in " ".join(args)
    monkeypatch.delenv(name)
    remove_token(config, provider_id)
    assert get_token(config, provider_id) == ""
    assert os.environ[legacy] == "synthetic-native-token"


def test_legacy_adapter_dotenv_key_is_read_and_moved_only_when_saved(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setenv("CLAUDE_CA_ENV_FILE", str(path))
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "synthetic-native-key")
    provider_id = "anthropic_auth"
    name = secret_var(provider_id)
    monkeypatch.delenv(name, raising=False)
    config = Config(path=tmp_path / "config.toml")
    original = 'ANTHROPIC_AUTH_TOKEN="synthetic-old-adapter-key"\nOTHER_TOKEN="keep"\n'
    path.write_text(original)
    assert get_token(config, provider_id) == "synthetic-old-adapter-key"
    assert path.read_text() == original
    store_token(config, provider_id, "synthetic-new-adapter-key")
    assert get_token(config, provider_id) == "synthetic-new-adapter-key"
    assert not any(line.startswith("ANTHROPIC_AUTH_TOKEN=") for line in path.read_text().splitlines())
    assert 'OTHER_TOKEN="keep"' in path.read_text()
    assert os.environ["ANTHROPIC_AUTH_TOKEN"] == "synthetic-native-key"
    remove_token(config, provider_id)
    assert get_token(config, provider_id) == ""
    assert 'OTHER_TOKEN="keep"' in path.read_text()


def test_removing_legacy_adapter_key_does_not_modify_native_environment(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setenv("CLAUDE_CA_ENV_FILE", str(path))
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "synthetic-native-key")
    path.write_text('ANTHROPIC_AUTH_TOKEN="synthetic-old-adapter-key"\nOTHER_TOKEN="keep"\n')
    remove_token(Config(path=tmp_path / "config.toml"), "anthropic_auth")
    assert "ANTHROPIC_AUTH_TOKEN" not in path.read_text()
    assert 'OTHER_TOKEN="keep"' in path.read_text()
    assert os.environ["ANTHROPIC_AUTH_TOKEN"] == "synthetic-native-key"


def test_generated_secret_collision_is_rejected_before_key_change(tmp_path):
    from claudapter.web import persist_provider_change

    first = "anthropic_auth"
    second = "claude_ca_provider_anthropic_auth"
    assert secret_var(first) == secret_var(second)
    config = Config({first: Provider(first, "One", "https://one.test/v1")}, path=tmp_path / "config.toml")
    updated = Config({**config.providers, second: Provider(second, "Two", "https://two.test/v1")}, path=config.path)
    with pytest.raises(ClaudapterError, match="collide"):
        persist_provider_change(config, updated, second, "synthetic-key")
    assert not config.path.exists()
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize("provider_id", ["123", "1-router", "9_router"])
def test_digit_leading_provider_keys_round_trip_in_safe_namespace(tmp_path, monkeypatch, provider_id):
    from claudapter.config import load, save

    monkeypatch.setenv("CLAUDE_CA_ENV_FILE", str(tmp_path / ".env"))
    name, legacy = secret_names(provider_id)
    assert name == "CLAUDE_CA_PROVIDER_" + legacy
    monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(legacy, "synthetic-legacy-environment")
    config = Config(
        {provider_id: Provider(provider_id, "Numeric", "https://example.test/v1")}, path=tmp_path / "config.toml"
    )
    save(config)
    assert get_token(config, provider_id) == ""
    store_token(config, provider_id, "synthetic-adapter-key")
    assert get_token(load(config.path), provider_id) == "synthetic-adapter-key"
    assert not any(line.startswith(legacy + "=") for line in (tmp_path / ".env").read_text().splitlines())
    remove_token(config, provider_id)
    assert get_token(config, provider_id) == ""
    assert os.environ[legacy] == "synthetic-legacy-environment"


def test_legacy_numeric_key_recovers_other_providers_and_migrates_on_save(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setenv("CLAUDE_CA_ENV_FILE", str(path))
    monkeypatch.delenv(secret_var("123"), raising=False)
    config = Config(path=tmp_path / "config.toml")
    original = '123_TOKEN="synthetic-old-key"\nOTHER_TOKEN="synthetic-other-key"\n'
    path.write_text(original)
    assert get_token(config, "123") == "synthetic-old-key"
    assert get_token(config, "other") == "synthetic-other-key"
    assert path.read_text() == original
    store_token(config, "123", "synthetic-new-key")
    assert get_token(config, "123") == "synthetic-new-key"
    assert get_token(config, "other") == "synthetic-other-key"
    assert not any(line.startswith("123_TOKEN=") for line in path.read_text().splitlines())


def test_legacy_numeric_key_can_be_removed_without_rewriting_other_keys(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setenv("CLAUDE_CA_ENV_FILE", str(path))
    path.write_text('123_TOKEN="synthetic-old-key"\nOTHER_TOKEN="synthetic-other-key"\n')
    config = Config(path=tmp_path / "config.toml")
    remove_token(config, "123")
    assert "123_TOKEN" not in path.read_text()
    assert get_token(config, "other") == "synthetic-other-key"


def test_numeric_safe_namespace_collision_is_rejected_before_key_write(tmp_path, monkeypatch):
    from claudapter.web import persist_provider_change

    monkeypatch.setenv("CLAUDE_CA_ENV_FILE", str(tmp_path / ".env"))
    first, second = "123", "claude_ca_provider_123"
    assert secret_var(first) == secret_var(second)
    config = Config({first: Provider(first, "One", "https://one.test/v1")}, path=tmp_path / "config.toml")
    updated = Config({**config.providers, second: Provider(second, "Two", "https://two.test/v1")}, path=config.path)
    with pytest.raises(ClaudapterError, match="collide"):
        persist_provider_change(config, updated, second, "synthetic-key")
    assert not config.path.exists() and not (tmp_path / ".env").exists()
