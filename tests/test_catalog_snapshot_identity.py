"""Cache identity describes the credential actually sent, even during edits."""

import importlib
import io
import json
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import pytest

PACKAGE = "claudapter"
catalog = importlib.import_module(PACKAGE + ".catalog")
config_module = importlib.import_module(PACKAGE + ".config")
credentials = importlib.import_module(PACKAGE + ".credentials")


@pytest.mark.parametrize("mode", ["direct", "gateway"])
def test_cache_entry_identity_uses_the_original_request_credential(tmp_path, monkeypatch, mode):
    discovery = pytest.importorskip("protoverta.discovery") if mode == "gateway" else None
    codex = PACKAGE == "codex_adapter"
    prefix = "CODEX_CA" if codex else "CLAUDE_CA"
    monkeypatch.setenv(prefix + "_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("FIXTURE_TOKEN", "synthetic-original")
    provider = config_module.Provider("fixture", "Fixture", "https://example.test/v1", execution_mode=mode)
    config = config_module.Config(providers={"fixture": provider}, path=tmp_path / "config.toml")
    if codex:
        provider.discovery.enabled = True
        provider.auth_secret_ref = "fixture-main"
        config.secrets["fixture-main"] = config_module.SecretReference("fixture-main", env_var="FIXTURE_TOKEN")

    def store(token):
        if codex:
            credentials.store_token("FIXTURE_TOKEN", token, config=config)
        else:
            credentials.store_token(config, "fixture", token)

    def identity():
        return catalog._identity(provider, config) if codex else catalog._identity(config, provider)

    store("synthetic-original")
    expected = identity()
    captured, entries = [], []

    class Response(io.BytesIO):
        def __init__(self, data):
            super().__init__(data)
            self.headers = {}

        def read(self, size=-1):
            store("synthetic-replacement")
            return super().read(size)

    def open_request(request, *, timeout):
        captured.append(request.get_header("Authorization"))
        return Response(json.dumps({"data": [{"id": "m"}]}).encode())

    def gateway_request(base, headers, policy, **options):
        captured.append(headers.get("Authorization"))
        store("synthetic-replacement")
        return [SimpleNamespace(as_dict=lambda: {"id": "m", "display_name": "M"})]

    with ExitStack() as stack:
        if discovery is not None:
            stack.enter_context(patch.object(discovery, "fetch_catalog", side_effect=gateway_request))
        opener = stack.enter_context(patch.object(catalog, "_open" if codex else "_OPENER"))
        if codex:
            opener.side_effect = open_request
        else:
            opener.open.side_effect = open_request
        catalog.fetch_models(config, provider, cache_sink=entries.append)
    assert captured == ["Bearer synthetic-original"]
    assert entries[0]["identity"] == expected
    assert entries[0]["identity"] != identity()
