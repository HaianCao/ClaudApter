import os
import sys
import types
from enum import StrEnum
from unittest.mock import patch

import pytest

from claudapter.catalog import _url, fetch_models
from claudapter.config import Config, Provider, load, save, validate
from claudapter.errors import ClaudapterError
from claudapter.launcher import _gateway, build_invocation
from claudapter.web import public_config


@pytest.mark.parametrize("protocol", ["chat", "responses"])
def test_cross_protocol_config_and_catalog(tmp_path, protocol):
    provider = Provider("demo", "My provider", "https://example.test/api/v4", execution_mode="gateway", protocol=protocol)
    config = Config({"demo": provider}, path=tmp_path / "config.toml")
    save(config)
    assert load(config.path).providers["demo"] == provider
    assert public_config(config)["providers"][0]["protocol"] == protocol
    assert _url(provider, "/v1/models") == "https://example.test/api/v4/models"
    provider.execution_mode = "direct"
    with pytest.raises(ClaudapterError, match="require"):
        validate(config)


def test_cross_protocol_launcher_settings_are_child_only(tmp_path):
    provider = Provider("demo", "Demo", "https://example.test/v1", execution_mode="gateway", protocol="chat")
    config = Config({"demo": provider}, path=tmp_path / "config.toml")
    with patch("claudapter.launcher.shutil.which", return_value="claude.exe"), patch("claudapter.launcher.get_token", return_value="secret"):
        before = dict(os.environ)
        args, env = build_invocation(config, provider, "m", "high", [], gateway_url="http://127.0.0.1:8765", gateway_token="local")
        assert os.environ == before
        assert env["MAX_THINKING_TOKENS"] == "0"
        assert env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] == "1"
        assert "--settings" in args
        with pytest.raises(ClaudapterError, match="effort"):
            build_invocation(config, provider, "m", "max", [], gateway_url="http://127.0.0.1:8765", gateway_token="local")


def test_loopback_proxy_bypass_does_not_change_parent_or_direct_mode(tmp_path):
    provider = Provider("demo", "Demo", "https://example.test", execution_mode="gateway", protocol="messages")
    config = Config({"demo": provider}, path=tmp_path / "config.toml")
    with patch("claudapter.launcher.shutil.which", return_value="claude.exe"), patch("claudapter.launcher.get_token", return_value="secret"), patch.dict(os.environ, {"NO_PROXY": "internal.test, other.test, localhost", "HTTPS_PROXY": "http://proxy.test:8080"}, clear=True):
        before = dict(os.environ)
        _args, env = build_invocation(config, provider, "m", "default", [], gateway_url="http://127.0.0.1:8765", gateway_token="local")
        assert dict(os.environ) == before
        assert env["NO_PROXY"] == env["no_proxy"]
        assert {"internal.test", "other.test", "localhost", "127.0.0.1", "::1"} <= set(env["NO_PROXY"].split(","))
        assert env["HTTPS_PROXY"] == before["HTTPS_PROXY"]
        provider.execution_mode = "direct"
        _args, direct = build_invocation(config, provider, "m", "default", [])
        assert direct["NO_PROXY"] == before["NO_PROXY"]


@pytest.mark.parametrize("protocol", ["messages", "chat", "responses"])
@pytest.mark.parametrize("prefix", ["/api", "/api/v1", "/api/v1/"])
def test_gateway_passes_explicit_prefix_and_compatibility_policy(tmp_path, protocol, prefix):
    class Protocol(StrEnum):
        MESSAGES = "messages"
        CHAT = "chat"
        RESPONSES = "responses"

    captured = {}

    def gateway_config(*args, **kwargs):
        captured.update(args=args, options=kwargs)
        return object()

    core = types.ModuleType("protoverta.core")
    core.Protocol = Protocol
    server = types.ModuleType("protoverta.server")
    server.GatewayConfig, server.gateway_session = gateway_config, lambda value: value
    provider = Provider("demo", "Demo", "https://example.test" + prefix, execution_mode="gateway", protocol=protocol)
    config = Config({"demo": provider}, path=tmp_path / "config.toml")
    with patch.dict(sys.modules, {"protoverta.core": core, "protoverta.server": server}), patch("claudapter.launcher.get_token", return_value="secret"):
        _gateway(config, provider)
    expected = "https://example.test" + prefix.rstrip("/")
    if protocol == "messages" and not expected.endswith("/v1"):
        expected += "/v1"
    assert captured["args"][1] == expected
    assert captured["options"] == ({"upstream_reasoning": "provider_default"} if protocol == "messages" else {"client_profile": "claude", "messages_usage_policy": "deferred", "upstream_reasoning": "provider_default"})


@pytest.mark.parametrize("protocol", ["chat", "responses"])
def test_discovery_does_not_send_anthropic_query_or_headers(tmp_path, protocol):
    provider = Provider("demo", "Demo", "https://example.test/api/v4", execution_mode="gateway", protocol=protocol)
    config = Config({"demo": provider}, path=tmp_path / "config.toml")
    from protoverta.discovery import ModelInfo

    with patch("claudapter.catalog.get_token", return_value="fake"), patch("protoverta.discovery.fetch_catalog", return_value=(ModelInfo("m", "Readable model"),)) as request:
        assert fetch_models(config, provider) == [{"id": "m", "display_name": "Readable model"}]
    assert request.call_args.args[0] == "https://example.test/api/v4"
    assert request.call_args.args[2].path == "/models"
    assert "anthropic-version" not in {key.lower() for key in request.call_args.args[1]}
