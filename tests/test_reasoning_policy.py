import sys
import types
from enum import StrEnum
from unittest.mock import Mock, patch

import pytest

from claudapter.config import Config, Provider, load, save, validate
from claudapter.errors import ClaudapterError
from claudapter.launcher import _gateway, build_invocation
from claudapter.web import public_config


def test_policy_persistence_and_gateway(tmp_path):
    provider = Provider("demo", "Demo", "https://example.test/v1", execution_mode="gateway", protocol="chat", upstream_reasoning="openrouter_disabled")
    config = Config({"demo": provider}, path=tmp_path / "config.toml")
    save(config)
    restored = load(config.path)
    assert restored.providers["demo"].upstream_reasoning == "openrouter_disabled"
    assert public_config(restored)["providers"][0]["upstream_reasoning"] == "openrouter_disabled"

    class Protocol(StrEnum):
        MESSAGES = "messages"
        CHAT = "chat"

    gateway_config = Mock()
    core = types.ModuleType("protoverta.core")
    core.Protocol = Protocol
    server = types.ModuleType("protoverta.server")
    server.GatewayConfig, server.gateway_session = gateway_config, Mock()
    with patch.dict(sys.modules, {"protoverta.core": core, "protoverta.server": server}), patch("claudapter.launcher.get_token", return_value="fake"):
        _gateway(config, provider)
    assert gateway_config.call_args.kwargs["upstream_reasoning"] == "openrouter_disabled"


def test_direct_policy_is_rejected(tmp_path):
    provider = Provider("demo", "Demo", "https://example.test", upstream_reasoning="disabled")
    config = Config({"demo": provider}, path=tmp_path / "config.toml")
    with pytest.raises(ClaudapterError, match="reasoning"):
        validate(config)
    with pytest.raises(ClaudapterError, match="reasoning"):
        build_invocation(config, provider, "m", "default", [])


def test_native_reasoning_override_does_not_disable_unrelated_features():
    provider = Provider("demo", "Demo", "https://example.test", execution_mode="gateway", upstream_reasoning="disabled")
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("claudapter.launcher.shutil.which", return_value="claude.exe"),
        patch("claudapter.launcher.get_token", return_value="fake"),
    ):
        args, env = build_invocation(Config(), provider, "m", "default", [], gateway_url="http://127.0.0.1:8765", gateway_token="local")
    assert env["MAX_THINKING_TOKENS"] == "0"
    assert "--settings" in args
    assert "DISABLE_PROMPT_CACHING" not in env
    assert "ENABLE_TOOL_SEARCH" not in env
    assert "CLAUDE_CODE_DISABLE_STRUCTURED_OUTPUTS" not in env


@pytest.mark.parametrize(
    "protocol,base,output",
    [
        ("messages", "https://example.test/v1", "reject"),
        ("chat", "https://openrouter.ai/api/v1", "omit"),
        ("chat", "https://integrate.api.nvidia.com/v1", "omit"),
        ("responses", "https://example.test/v1", "omit"),
    ],
)
def test_auto_reasoning_keeps_provider_default_and_omits_unrepresentable_output(tmp_path, monkeypatch, protocol, base, output):
    # Live checks: disabling reasoning returned HTTP 400 for mandatory-reasoning
    # models and was ignored by others, so auto never sends a disable request.
    pytest.importorskip("protoverta.automatic_reasoning")
    provider = Provider("demo", "Demo", base, protocol=protocol, execution_mode="gateway", upstream_reasoning="auto")
    config = Config({"demo": provider}, path=tmp_path / "config.toml")
    monkeypatch.setattr("claudapter.launcher.get_token", lambda *_: "synthetic")
    gateway, _ = _gateway(config, provider)
    assert gateway.upstream_reasoning == "provider_default"
    assert gateway.reasoning_output == output and gateway.public_reasoning is False
    assert provider.upstream_reasoning == "auto"
    save(config)
    assert load(config.path).providers["demo"].upstream_reasoning == "auto"


def test_direct_auto_does_not_disable_native_thinking(monkeypatch):
    monkeypatch.setattr("claudapter.launcher.shutil.which", lambda _: "claude.exe")
    monkeypatch.setattr("claudapter.launcher.get_token", lambda *_: "synthetic")
    monkeypatch.delenv("MAX_THINKING_TOKENS", raising=False)
    provider = Provider("demo", "Demo", "https://example.test/v1", upstream_reasoning="auto")
    args, env = build_invocation(Config(), provider, "m", "high", [])
    assert "MAX_THINKING_TOKENS" not in env and "--settings" not in args
