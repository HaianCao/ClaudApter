"""Invalid credentials fail before writes, provider IO or child processes."""

import json
from unittest.mock import patch

import pytest

from claudapter.catalog import fetch_models
from claudapter.config import Config, Provider, secret_var
from claudapter.credentials import get_token, store_token
from claudapter.errors import ClaudapterError
from claudapter.launcher import preflight
from claudapter.web import public_config


@pytest.mark.parametrize("character", ["\0", "\r", "\n", "\t", "\x1f", "\x7f"])
@pytest.mark.parametrize("position", ["start", "middle", "end"])
def test_control_characters_rejected_without_changing_saved_key(tmp_path, monkeypatch, character, position):
    path = tmp_path / ".env"
    monkeypatch.setenv("CLAUDE_CA_ENV_FILE", str(path))
    config = Config(path=tmp_path / "config.toml")
    store_token(config, "fixture", "synthetic-original")
    before = path.read_bytes()
    token = {
        "start": character + "synthetic",
        "middle": "synthetic" + character + "key",
        "end": "synthetic" + character,
    }[position]
    with pytest.raises(ClaudapterError, match="control characters") as error:
        store_token(config, "fixture", token)
    assert "synthetic" not in str(error.value)
    assert path.read_bytes() == before


@pytest.mark.parametrize("mode", ["direct", "gateway"])
@pytest.mark.parametrize("source", ["file", "environment"])
def test_invalid_existing_key_fails_preflight_and_discovery_but_setup_can_recover(tmp_path, monkeypatch, mode, source):
    if mode == "gateway":
        pytest.importorskip("protoverta")
    path = tmp_path / ".env"
    monkeypatch.setenv("CLAUDE_CA_ENV_FILE", str(path))
    name = secret_var("fixture")
    monkeypatch.delenv(name, raising=False)
    invalid = "synthetic\x00key" if source == "file" else "synthetic\nkey"
    if source == "file":
        path.write_text(name + "=" + json.dumps(invalid) + "\n")
    else:
        monkeypatch.setenv(name, invalid)
    provider = Provider("fixture", "Fixture", "https://example.test/v1", execution_mode=mode)
    config = Config({"fixture": provider}, path=tmp_path / "config.toml")
    monkeypatch.setattr("claudapter.launcher.shutil.which", lambda _: "claude.exe")
    before = path.read_bytes() if path.exists() else None
    with patch("claudapter.launcher.subprocess.run") as child, patch("claudapter.catalog.request_json") as request:
        with pytest.raises(ClaudapterError, match="control characters"):
            preflight(config, provider, "m", "default")
        with pytest.raises(ClaudapterError, match="control characters"):
            fetch_models(config, provider)
        child.assert_not_called()
        request.assert_not_called()
    assert (path.read_bytes() if path.exists() else None) == before
    assert public_config(config)["providers"][0]["has_key"] is True
    store_token(config, "fixture", "synthetic-replacement")
    assert get_token(config, "fixture") == "synthetic-replacement"
