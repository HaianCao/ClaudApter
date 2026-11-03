import json
import os

import pytest

from claudapter import agent_models, launcher
from claudapter.config import Config, Provider, load, save
from claudapter.errors import ClaudapterError


def make_config(tmp_path, options):
    provider = Provider("fixture", "Fixture", "https://example.invalid/v1", agent_models=options)
    config = Config(providers={"fixture": provider}, path=tmp_path / "config.toml")
    return config, provider


@pytest.mark.parametrize("options", [{}, {"policy": "inherit"}, {"policy": "selected"}, {"policy": "custom", "default": "provider/worker", "haiku": "provider/fast", "fable": "provider/other"}])
def test_config_roundtrip_and_public_snapshot(tmp_path, monkeypatch, options):
    from claudapter import web
    monkeypatch.setattr(web, "get_token", lambda *_: None)
    config, _ = make_config(tmp_path, options)
    save(config)
    restored = load(config.path)
    assert restored.providers["fixture"].agent_models == options
    assert web.public_config(restored)["providers"][0]["agent_models"] == options
    if not options:
        assert "agent_models" not in config.path.read_text(encoding="utf-8")


@pytest.mark.parametrize("value", [None, [], True, "selected", {"policy": []}, {"policy": "bad"},
    {"policy": "inherit", "default": "m"}, {"policy": "selected", "default": "m"},
    {"policy": "custom"}, {"policy": "custom", "default": ""}, {"policy": "custom", "default": "bad model"},
    {"policy": "custom", "default": "a\x00b"}, {"policy": "custom", "default": chr(0xD800)},
    {"policy": "custom", "default": "x" * 513}, {"unknown": "x"},
    {"policy": "custom", "default": "haiku"}, {"policy": "custom", "effort": "high"}])
def test_invalid_policy_never_overwrites_config(tmp_path, value):
    config, provider = make_config(tmp_path, {})
    save(config)
    before = config.path.read_bytes()
    provider.agent_models = value
    with pytest.raises(ClaudapterError):
        save(config)
    assert config.path.read_bytes() == before


def test_legacy_missing_policy_is_preserved(tmp_path):
    config, _ = make_config(tmp_path, {})
    save(config)
    loaded = load(config.path)
    assert loaded.providers["fixture"].agent_models == {}
    monkey_options = agent_models.agent_environment([], {}, 'selected')
    assert monkey_options == {}


def test_inherit_preserves_external_agent_choices_without_version_probe(tmp_path, monkeypatch):
    config, provider = make_config(tmp_path, {"policy": "inherit"})
    monkeypatch.setattr(agent_models, "detect_version", lambda _: pytest.fail("inherit must not probe"))
    monkeypatch.setattr(launcher.shutil, "which", lambda _: "fixture.exe")
    monkeypatch.setattr(launcher, "get_token", lambda *_: "synthetic")
    monkeypatch.setenv("ANTHROPIC_DEFAULT_HAIKU_MODEL", "existing-fast")
    monkeypatch.setenv("CLAUDE_CODE_SUBAGENT_MODEL_FORCE", "1")
    args, env = launcher.build_invocation(config, provider, "provider/main", "high", [])
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "existing-fast"
    assert env["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"] == "1"
    assert not any("default_subagent" in item for item in args)


def test_exact_model_quotes_roundtrip_without_injection(tmp_path):
    model = 'provider/<tag>"quoted"'
    options = {"policy": "custom", "default": model}
    config, _ = make_config(tmp_path, options)
    save(config)
    assert load(config.path).providers["fixture"].agent_models["default"] == model


@pytest.mark.parametrize("gateway", [False, True])
def test_selected_policy_is_child_only_and_keeps_auth_and_main_effort(tmp_path, monkeypatch, gateway):
    config, provider = make_config(tmp_path, {"policy": "selected"})
    monkeypatch.setattr(agent_models, "detect_version", lambda _: (2, 1, 308))
    monkeypatch.setattr(launcher.shutil, "which", lambda _: "fixture.exe")
    monkeypatch.setattr(launcher, "get_token", lambda *_: "synthetic-provider")
    monkeypatch.setenv("ANTHROPIC_DEFAULT_HAIKU_MODEL", "old-model")
    monkeypatch.setenv("CLAUDE_CODE_SUBAGENT_MODEL_FORCE", "1")
    monkeypatch.setenv("UNRELATED_SETTING", "preserve-me")
    if gateway:
        provider.execution_mode = "gateway"
        provider.protocol = "chat"
        monkeypatch.setattr(launcher, "cross_protocol_environment", lambda _: {})
    before = dict(os.environ)
    args, env = launcher.build_invocation(config, provider, "provider/selected", "high", [],
        **({"gateway_url": "http://127.0.0.1:4567", "gateway_token": "synthetic-local"} if gateway else {}))
    assert os.environ == before
    assert env["UNRELATED_SETTING"] == "preserve-me"
    assert "provider/selected" in args
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "provider/selected"
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "provider/selected"
    assert env["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"] == "1"
    settings = json.loads(args[args.index("--settings") + 1])
    assert settings["env"]["ANTHROPIC_DEFAULT_FABLE_MODEL"] == "provider/selected"
    assert env["ANTHROPIC_AUTH_TOKEN"] == ("synthetic-local" if gateway else "synthetic-provider")
    assert args[args.index("--effort") + 1] == "high"
    if gateway:
        assert settings["alwaysThinkingEnabled"] is False


def test_explicit_policy_on_unknown_version_fails_before_launch(tmp_path, monkeypatch):
    config, provider = make_config(tmp_path, {"policy": "selected"})
    monkeypatch.setattr(agent_models, "detect_version", lambda _: None)
    monkeypatch.setattr(launcher.shutil, "which", lambda _: "fixture.exe")
    monkeypatch.setattr(launcher, "get_token", lambda *_: "synthetic-provider")
    with pytest.raises(ClaudapterError, match="Inherit"):
        launcher.build_invocation(config, provider, "provider/selected", "high", [])


def test_custom_policy_has_explicit_precedence_and_effort(monkeypatch):
    monkeypatch.setattr(agent_models, "detect_version", lambda _: (2, 1, 308))
    values = agent_models.agent_environment(["fixture"], {"policy": "custom", "haiku": "provider/fast", "default": "provider/worker"}, "provider/main")
    assert values["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"] == "0"
    assert values["CLAUDE_CODE_SUBAGENT_MODEL"] == "provider/worker"
    assert values["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "provider/fast"
    assert values["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "provider/main"
    assert "CLAUDE_CODE_EFFORT_LEVEL" not in values
