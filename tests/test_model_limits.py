"""Provider model limits and Claude Code refusal-fallback environment."""

import pytest

from claudapter import launcher
from claudapter.config import Config, Provider, load, model_limit_environment, save, validate_model_limits
from claudapter.errors import ClaudapterError


def test_model_limits_validate_round_trip_and_map_to_claude_environment(tmp_path):
    provider = Provider(
        "demo", "Demo", "https://example.test", model_limits={"context_window": 128000, "max_output_tokens": 32000}
    )
    config = Config({"demo": provider}, path=tmp_path / "config.toml")
    save(config)
    assert load(config.path).providers["demo"].model_limits == {"context_window": 128000, "max_output_tokens": 32000}
    assert model_limit_environment(provider.model_limits) == {
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "128000",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "32000",
    }
    assert model_limit_environment({}) == {}


@pytest.mark.parametrize(
    "value",
    [
        {"context_window": 0},
        {"context_window": True},
        {"context_window": "128000"},
        {"other": 1},
        {"context_window": 1000, "max_output_tokens": 1000},
    ],
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


def _cache_limits(monkeypatch, limits):
    from claudapter import catalog

    monkeypatch.setattr(catalog, "cached_model_limits", lambda *_args, **_kwargs: limits)


def test_reported_limits_read_provider_fields_and_ignore_invalid_values():
    from claudapter.catalog import reported_limits

    openrouter = {
        "id": "m",
        "context_length": 1_000_000,
        "top_provider": {"context_length": 1_000_000, "max_completion_tokens": 65536},
    }
    assert reported_limits(openrouter) == {"context_window_tokens": 1_000_000, "max_output_tokens": 65536}
    assert reported_limits({"id": "m", "context_length": 1_048_576}) == {
        "context_window_tokens": 1_048_576,
        "max_output_tokens": None,
    }
    assert reported_limits({"id": "m"}) == {"context_window_tokens": None, "max_output_tokens": None}
    assert reported_limits({"context_length": "128000", "max_output_tokens": True}) == {
        "context_window_tokens": None,
        "max_output_tokens": None,
    }
    assert reported_limits({"context_window_tokens": 200000, "max_output_tokens": 8192}) == {
        "context_window_tokens": 200000,
        "max_output_tokens": 8192,
    }


def test_launch_limits_take_model_maximums_but_leave_room_for_input():
    from claudapter.catalog import launch_limits

    assert launch_limits({"context_window_tokens": 1_000_000, "max_output_tokens": 65536}) == {
        "context_window": 1_000_000,
        "max_output_tokens": 65536,
    }
    # An output maximum close to the whole window would leave no room for input.
    assert launch_limits({"context_window_tokens": 1_048_576, "max_output_tokens": 943_718}) == {
        "context_window": 1_048_576,
        "max_output_tokens": 524_288,
    }
    assert launch_limits({"context_window_tokens": 1_048_576, "max_output_tokens": None}) == {
        "context_window": 1_048_576
    }
    assert launch_limits({}) == {}


def test_launch_uses_reported_model_limits_unless_the_provider_sets_them(monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda _: "claude.exe")
    monkeypatch.setattr(launcher, "get_token", lambda *_: "synthetic")
    _cache_limits(monkeypatch, {"context_window_tokens": 1_000_000, "max_output_tokens": 65536})
    provider = Provider("demo", "Demo", "https://example.test")
    _, env = launcher.build_invocation(Config({"demo": provider}), provider, "vendor/model", "default", [])
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "1000000" and env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "65536"
    manual = Provider("demo", "Demo", "https://example.test", model_limits={"context_window": 100000})
    _, env = launcher.build_invocation(Config({"demo": manual}), manual, "vendor/model", "default", [])
    # The manual window wins; the reported output no longer fits it and is halved.
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "100000" and env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "50000"


def test_launch_sets_no_limits_when_the_provider_reports_none(monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda _: "claude.exe")
    monkeypatch.setattr(launcher, "get_token", lambda *_: "synthetic")
    _cache_limits(monkeypatch, {})
    provider = Provider("demo", "Demo", "https://example.test")
    _, env = launcher.build_invocation(Config({"demo": provider}), provider, "vendor/model", "default", [])
    assert "CLAUDE_CODE_MAX_CONTEXT_TOKENS" not in env and "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in env


def test_direct_catalog_keeps_reported_limits_in_the_cache(tmp_path, monkeypatch):
    from claudapter import catalog

    provider = Provider("demo", "Demo", "https://example.test")
    config = Config({"demo": provider}, path=tmp_path / "config.toml")
    monkeypatch.setattr(catalog, "get_token", lambda *_args, **_kwargs: "synthetic")
    monkeypatch.setattr(
        catalog,
        "request_json",
        lambda *_args, **_kwargs: {
            "data": [
                {"id": "vendor/model", "context_length": 262144, "top_provider": {"max_completion_tokens": 32768}}
            ],
            "has_more": False,
        },
    )
    catalog.fetch_models(config, provider)
    assert catalog.cached_model_limits(config, provider, "vendor/model") == {
        "context_window_tokens": 262144,
        "max_output_tokens": 32768,
    }
    assert catalog.cached_model_limits(config, provider, "other/model") == {}
