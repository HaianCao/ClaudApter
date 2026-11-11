"""Where settings and caches live on Windows, Linux and macOS."""

from pathlib import Path

import pytest

from claudapter.config import cache_path, config_path


@pytest.fixture
def unix_home(monkeypatch, tmp_path):
    for name in ("APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "CLAUDE_CA_CONFIG", "CLAUDE_CA_CACHE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def test_linux_and_macos_default_to_dot_config_and_dot_cache(unix_home):
    assert config_path() == unix_home / ".config" / "claudapter" / "config.toml"
    assert cache_path() == unix_home / ".cache" / "claudapter" / "models.json"


def test_absolute_xdg_directories_are_honoured(unix_home, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cch"))
    assert config_path() == tmp_path / "cfg" / "claudapter" / "config.toml"
    assert cache_path() == tmp_path / "cch" / "claudapter" / "models.json"


def test_relative_xdg_directories_are_ignored(unix_home, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/cfg")
    monkeypatch.setenv("XDG_CACHE_HOME", "relative/cache")
    assert config_path() == unix_home / ".config" / "claudapter" / "config.toml"
    assert cache_path() == unix_home / ".cache" / "claudapter" / "models.json"


def test_windows_appdata_still_wins(unix_home, monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert config_path() == tmp_path / "Roaming" / "claudapter" / "config.toml"
    assert cache_path() == tmp_path / "Local" / "claudapter" / "models.json"
