import sys
import types
from enum import StrEnum
from unittest.mock import Mock, patch

import pytest

from claudapter.config import Config, Provider, load, save
from claudapter.errors import ClaudapterError
from claudapter.launcher import _gateway, preflight
from claudapter.web import public_config


def test_registry_settings_persist_and_require_gateway(tmp_path):
    provider = Provider("demo", "Demo", "https://example.test/v1", protocol="chat", execution_mode="gateway", capabilities_file="checks.json", require_verified_capabilities=True)
    config = Config({"demo": provider}, path=tmp_path / "config.toml")
    save(config)
    restored = load(config.path)
    assert restored.providers["demo"] == provider
    assert public_config(restored)["providers"][0]["require_verified_capabilities"] is True
    provider.execution_mode = "direct"
    with pytest.raises(ClaudapterError, match="gateway"):
        save(config)
    provider.execution_mode = "gateway"
    provider.capabilities_file = ""
    with pytest.raises(ClaudapterError, match="registry"):
        save(config)
    provider.require_verified_capabilities = "false"
    with pytest.raises(ClaudapterError, match="boolean"):
        save(config)


def test_messages_registry_uses_actual_v1_prefix_and_config_relative_path(tmp_path):
    class Protocol(StrEnum):
        MESSAGES = "messages"

    provider = Provider("demo", "Demo", "https://example.test/api", execution_mode="gateway", capabilities_file="checks.json", require_verified_capabilities=True)
    config = Config({"demo": provider}, path=tmp_path / "config.toml")
    core = types.ModuleType("protoverta.core")
    core.Protocol = Protocol
    server = types.ModuleType("protoverta.server")
    server.GatewayConfig, server.gateway_session = Mock(), Mock()
    capabilities = types.ModuleType("protoverta.capabilities")
    capabilities.CapabilityRegistry = Mock()
    with patch.dict(sys.modules, {"protoverta.core": core, "protoverta.server": server, "protoverta.capabilities": capabilities}), patch("claudapter.launcher.get_token", return_value="fixture-key"):
        _gateway(config, provider)
    capabilities.CapabilityRegistry.load.assert_called_once_with(tmp_path / "checks.json")
    assert server.GatewayConfig.call_args.args[1] == "https://example.test/api/v1"
    assert server.GatewayConfig.call_args.kwargs["require_verified_capabilities"] is True


def test_failed_preflight_does_not_open_listener_or_launch_cli():
    provider = Provider("demo", "Demo", "https://example.test", execution_mode="gateway")
    report = Mock()
    report.require.side_effect = ValueError("unverified=streaming")
    registry = Mock()
    registry.assess.return_value = report
    gateway = types.SimpleNamespace(capabilities=registry, upstream_base_url=provider.base_url + "/v1", upstream_protocol="messages", upstream_reasoning="provider_default", require_verified_capabilities=True)
    listener = Mock()
    with patch("claudapter.launcher._gateway", return_value=(gateway, listener)), patch("claudapter.launcher.build_invocation") as build:
        with pytest.raises(ClaudapterError, match="unverified=streaming"):
            preflight(Config(), provider, "model-a", "default")
    listener.assert_not_called()
    build.assert_not_called()
    assert registry.assess.call_args.args[2] == "model-a"
