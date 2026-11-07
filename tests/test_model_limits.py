"""Provider model limits and Claude Code refusal-fallback environment."""

import pytest

from claudapter import launcher
from claudapter.config import Config, Provider, load, model_limit_environment, save, validate_model_limits
from claudapter.errors import ClaudapterError


def test_model_limits_validate_round_trip_and_map_to_claude_environment(tmp_path):
    provider = Provider("demo", "Demo", "https://example.test", model_limits={"context_window": 128000, "max_output_tokens": 32000})
    config = Config({"demo": provider}, path=tmp_path / "config.toml")
    save(config)
    assert load(config.path).providers["demo"].model_limits == {"context_window": 128000, "max_output_tokens": 32000}
    assert model_limit_environment(provider.model_limits) == {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "128000", "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "32000"}
    assert model_limit_environment({}) == {}


@pytest.mark.parametrize(
    "value",
    [{"context_window": 0}, {"context_window": True}, {"context_window": "128000"}, {"other": 1}, {"context_window": 1000, "max_output_tokens": 1000}],
)
def test_invalid_model_limits_are_rejected(value):
    with pytest.raises(ClaudapterError):
        validate_model_limits(value)


def test_launch_sets_limits_and_disables_refusal_model_fallback_unless_user_chose(monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda _: "claude.exe")
    monkeypatch.setattr(launcher, "get_token", lambda *_: "synthetic")
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK", raising=False)
    provider = Provider("demo", "Demo", "https://example.test", model_limits={"context_window": 200000})
    config = Config({"demo": provider})
    _, env = launcher.build_invocation(config, provider, "vendor/model", "default", [])
    assert env["CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK"] == "1"
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "200000" and "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in env
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK", "0")
    _, env = launcher.build_invocation(config, provider, "vendor/model", "default", [])
    assert env["CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK"] == "0"
