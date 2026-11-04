import json

import pytest

from claudapter.config import Config, Provider
from claudapter.model_selection import model_status


def test_setup_status_uses_exact_route_and_keeps_missing_evidence_unknown(tmp_path, monkeypatch):
    pytest.importorskip("protoverta")
    from protoverta.capabilities import (
        CapabilityRecord,
        CapabilityRegistry,
        RouteCapabilityRecord,
        route_policies,
    )
    from protoverta.server import GatewayConfig

    provider = Provider("p", "P", "https://fixture.test/v1", protocol="chat", execution_mode="gateway", capabilities_file="evidence.json")
    config = Config(providers={"p": provider}, path=tmp_path / "config.toml")
    gateway = GatewayConfig("chat", provider.base_url, "key", "local", client_profile="claude", messages_usage_policy="deferred")
    good = CapabilityRecord(provider.base_url, "chat", "ok", {"text": True, "streaming": True}, "2026-11-04", "fixture", context_window_tokens=32768)
    bad = CapabilityRecord(provider.base_url, "chat", "bad", {"streaming": False}, "2026-11-04", "fixture")
    route = RouteCapabilityRecord(provider.base_url, "chat", "ok", "messages", True, "claude", route_policies(gateway), good.features, "2026-11-04", "fixture", "fixture")
    registry = CapabilityRegistry((good, bad), (route,))
    monkeypatch.setattr(CapabilityRegistry, "load", lambda path: registry)
    ok_status = model_status(config, provider, "ok")
    assert ok_status["status"] == "verified"
    assert ok_status["count_category"] == "unavailable"
    assert ok_status["context_window_tokens"] == 32768
    assert "Token count: unavailable; context window: 32768." in ok_status["message"]
    assert model_status(config, provider, "bad")["status"] == "unsupported"
    assert model_status(config, provider, "unknown")["status"] == "unverified"
    provider.gateway_runtime["context_preflight"] = "estimate"
    assert model_status(config, provider, "ok")["count_category"] == "estimate"
    provider.gateway_runtime.pop("context_preflight")
    provider.gateway_runtime["require_current_evidence"] = True
    assert model_status(config, provider, "ok")["status"] == "unverified"
    assert model_status(config, provider, "bad")["status"] == "unsupported"


def test_picker_is_session_scoped_and_does_not_fake_claude_capabilities(tmp_path, monkeypatch):
    from claudapter import agent_models, catalog, harness, launcher

    provider = Provider("p", "P", "https://fixture.test/v1", protocol="chat", execution_mode="gateway", agent_models={"policy": "custom", "default": "worker"})
    config = Config(providers={"p": provider}, path=tmp_path / "config.toml")
    monkeypatch.setattr(launcher.shutil, "which", lambda _: "claude.exe")
    monkeypatch.setattr(launcher, "command_prefix", lambda *_: ["claude.exe"])
    monkeypatch.setattr(launcher, "get_token", lambda *_: "synthetic")
    monkeypatch.setattr(harness, "detect_version", lambda _: (2, 1, 308))
    monkeypatch.setattr(agent_models, "detect_version", lambda _: (2, 1, 308))
    monkeypatch.setattr(catalog, "cached_model_names", lambda *_args, **_kwargs: {"main": "Friendly", "worker": "Worker"})
    args, _ = launcher.build_invocation(config, provider, "main", "default", [], gateway_url="http://127.0.0.1:1", gateway_token="local")
    settings = json.loads(args[args.index("--settings") + 1])
    assert settings["modelPicker"]["replaceBuiltInOptions"] is True
    assert [(item["model"], item["label"]) for item in settings["modelPicker"]["options"]] == [("main", "Friendly"), ("worker", "Worker")]
    assert "behavesAs" not in json.dumps(settings) and "availableModels" not in settings
    assert not config.path.exists()
