"""Malformed but parseable config fails safely at load/save and CLI boundaries."""

from dataclasses import replace

import pytest

from claudapter.cli import main
from claudapter.config import Config, Provider, load, save, validate
from claudapter.errors import ClaudapterError


@pytest.mark.parametrize("content", [
    'schema_version = true',
    'schema_version = "1"',
    'schema_version = 1\ndefaults = []',
    'schema_version = 1\ndefaults = "bad"',
    'schema_version = 1\nproviders = "bad"',
    'schema_version = 1\nproviders = {}',
    'schema_version = 1\nproviders = [123]',
    'schema_version = 1\nproviders = [[]]',
    'schema_version = 1\n[defaults]\nprovider = []',
    'schema_version = 1\n[defaults]\nmodel = 123',
    'schema_version = 1\n[defaults]\neffort = []',
    'schema_version = 1\n[[providers]]\nid = []\ndisplay_name = "Fixture"\nbase_url = "https://example.test/v1"',
    'schema_version = 1\n[[providers]]\nid = "fixture"\ndisplay_name = 123\nbase_url = "https://example.test/v1"',
    'schema_version = 1\n[[providers]]\nid = "fixture"\ndisplay_name = "Fixture"\nbase_url = false',
    'schema_version = 1\n[[providers]]\nid = "fixture"\ndisplay_name = "Fixture"\nbase_url = "https://example.test/v1"\nprotocol = []',
    'schema_version = 1\n[[providers]]\nid = "fixture"',
])
def test_invalid_toml_types_return_controlled_errors_without_rewriting(tmp_path, monkeypatch, capsys, content):
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")
    before = path.read_bytes()
    monkeypatch.setenv("CLAUDE_CA_CONFIG", str(path))
    with pytest.raises(ClaudapterError):
        load(path)
    assert main(["validate"]) == 1
    error = capsys.readouterr().err
    assert error.startswith("claude-ca: ") and "Traceback" not in error
    assert path.read_bytes() == before


@pytest.mark.parametrize("field_name", ["id", "display_name", "base_url", "auth_mode", "default_model", "execution_mode", "protocol", "upstream_reasoning", "capabilities_file", "require_verified_capabilities", "gateway_limits", "gateway_runtime"])
@pytest.mark.parametrize("invalid", [None, [], 123])
def test_invalid_provider_field_types_do_not_overwrite_valid_config(tmp_path, field_name, invalid):
    provider = Provider("fixture", "Fixture", "https://example.test/v1")
    config = Config({"fixture": provider}, path=tmp_path / "config.toml")
    save(config)
    before = config.path.read_bytes()
    config.providers["fixture"] = replace(provider, **{field_name: invalid})
    with pytest.raises(ClaudapterError):
        save(config)
    assert config.path.read_bytes() == before


@pytest.mark.parametrize("config", [Config(providers=[]), Config(providers={"fixture": {}}), Config(providers={123: Provider("fixture", "Fixture", "https://example.test/v1")}), Config(default_provider=[]), Config(default_model=123), Config(default_effort=[])])
def test_invalid_programmatic_config_is_rejected(config):
    with pytest.raises(ClaudapterError):
        validate(config)


def test_valid_optional_defaults_remain_compatible(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('schema_version = 1\n[[providers]]\nid="fixture"\ndisplay_name="Fixture"\nbase_url="https://example.test/v1"\n')
    config = load(path)
    assert config.default_model == "" and config.providers["fixture"].upstream_reasoning == "auto"
    save(config)
    assert load(path) == config
