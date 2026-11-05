import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from claudapter.mcp import environment_references, load_servers, session_arguments


def config(tmp_path):
    path = tmp_path / "servers.json"
    path.write_text(
        json.dumps({"mcpServers": {"search": {"command": sys.executable, "args": ["-m", "example"], "env_vars": ["SEARCH_TOKEN"]}}}), encoding="utf-8"
    )
    return str(path)


def test_mcp_snapshot_is_session_scoped_and_deleted_on_error(tmp_path):
    value = config(tmp_path)
    with pytest.raises(RuntimeError, match="child failed"), session_arguments(value, {"SEARCH_TOKEN": "synthetic-key", "OTHER_KEY": "not-forwarded"}) as args:
        assert args[:2] == ["--strict-mcp-config", "--mcp-config"]
        path = Path(args[2])
        document = json.loads(path.read_text())
        assert document["mcpServers"]["search"]["env"] == {"SEARCH_TOKEN": "${SEARCH_TOKEN}"}
        assert "synthetic-key" not in path.read_text()
        raise RuntimeError("child failed")
    assert not path.exists()
    assert Path(value).exists()


def test_mcp_requires_named_variables_and_is_opt_in(tmp_path):
    with pytest.raises(ValueError, match="missing from the child"), session_arguments(config(tmp_path), {}):
        pytest.fail("missing variable must not launch a server")
    with session_arguments(None, {}) as args:
        assert args == []


def test_inline_secrets_are_rejected(tmp_path):
    path = tmp_path / "servers.json"
    path.write_text(json.dumps({"mcpServers": {"search": {"command": sys.executable, "env": {"SECRET": "do-not-echo"}}}}))
    with pytest.raises(ValueError) as failure:
        load_servers(str(path))
    assert "do-not-echo" not in str(failure.value)


def test_npm_prefix_stays_intact_and_snapshot_lives_until_child_exit(tmp_path, monkeypatch):
    from claudapter import launcher
    from claudapter.config import Config, Provider

    provider = Provider("demo", "Demo", "https://example.invalid")
    monkeypatch.setenv("CLAUDE_CA_MCP_CONFIG", config(tmp_path))
    monkeypatch.setenv("SEARCH_TOKEN", "synthetic-key")
    monkeypatch.setattr(launcher.shutil, "which", lambda _: "claude.cmd")
    monkeypatch.setattr(launcher, "command_prefix", lambda *_: ["node.exe", "cli.js"])
    monkeypatch.setattr(launcher, "get_token", lambda *_: "synthetic-provider-key")
    snapshots = []

    def child(args, **kwargs):
        assert args[:2] == ["node.exe", "cli.js"]
        path = Path(args[args.index("--mcp-config") + 1])
        assert path.is_file()
        assert args.index("--mcp-config") < args.index("--model")
        assert "synthetic-key" not in " ".join(args)
        snapshots.append(path)
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(launcher.subprocess, "run", child)
    assert launcher.launch(Config({"demo": provider}, path=tmp_path / "config.toml"), provider, "model", "default", ["hello"]) == 7
    assert snapshots and not snapshots[0].exists()


def test_environment_lookup_follows_windows_case_rules_without_storing_values():
    servers = {"search": {"env_vars": ["search_token"]}}
    assert environment_references(servers, {"SEARCH_TOKEN": "synthetic-key"}, case_sensitive=False) == {
        "search": {"search_token": "${SEARCH_TOKEN}"},
    }
    with pytest.raises(ValueError, match="missing from the child"):
        environment_references(servers, {"SEARCH_TOKEN": "synthetic-key"}, case_sensitive=True)


def test_preflight_rejects_missing_mcp_env_without_snapshot_or_child(tmp_path, monkeypatch):
    from claudapter import launcher
    from claudapter.config import Config, Provider
    from claudapter.errors import ClaudapterError

    monkeypatch.setenv("CLAUDE_CA_MCP_CONFIG", config(tmp_path))
    monkeypatch.delenv("SEARCH_TOKEN", raising=False)
    monkeypatch.setattr(launcher.shutil, "which", lambda _: "claude.exe")
    monkeypatch.setattr(launcher, "get_token", lambda *_: "synthetic")

    def forbidden(*args, **kwargs):
        pytest.fail("preflight must not create an MCP snapshot or start a process")

    monkeypatch.setattr(launcher.subprocess, "run", forbidden)
    monkeypatch.setattr("claudapter.mcp.tempfile.TemporaryDirectory", forbidden)
    provider = Provider("demo", "Demo", "https://example.test/v1")
    with pytest.raises(ClaudapterError, match="missing from the child"):
        launcher.preflight(Config({"demo": provider}, path=tmp_path / "config.toml"), provider, "m", "default")


def test_preflight_checks_sanitized_child_env_not_parent(tmp_path, monkeypatch):
    from claudapter import launcher
    from claudapter.config import Config, Provider
    from claudapter.errors import ClaudapterError

    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"search": {"command": sys.executable, "env_vars": ["ANTHROPIC_API_KEY"]}}}))
    monkeypatch.setenv("CLAUDE_CA_MCP_CONFIG", str(path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-parent")
    monkeypatch.setattr(launcher.shutil, "which", lambda _: "claude.exe")
    monkeypatch.setattr(launcher, "get_token", lambda *_: "synthetic")
    provider = Provider("demo", "Demo", "https://example.test/v1", auth_mode="bearer")
    with pytest.raises(ClaudapterError, match="missing from the child"):
        launcher.preflight(Config({"demo": provider}, path=tmp_path / "config.toml"), provider, "m", "default")
