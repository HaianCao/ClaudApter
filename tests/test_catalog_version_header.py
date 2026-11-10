"""Catalog header contracts over real loopback HTTP, no provider credentials."""

import importlib
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

PACKAGE = "claudapter"
catalog = importlib.import_module(PACKAGE + ".catalog")
configuration = importlib.import_module(PACKAGE + ".config")
CODEX = PACKAGE == "codex_adapter"
VERSION = "2023-06-01"


@contextmanager
def provider_server(protocol, auth, paginated):
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            headers = {name.lower(): value for name, value in self.headers.items()}
            observed.append((self.path, headers))
            valid = headers.get("anthropic-version") == (VERSION if protocol == "messages" else None)
            valid &= headers.get("authorization") == ("Bearer synthetic-key" if auth == "bearer" else None)
            valid &= headers.get("x-api-key") == ("synthetic-key" if auth == "api_key" else None)
            valid &= urlsplit(self.path).path == "/custom/v1/models"
            page = parse_qs(urlsplit(self.path).query).get("after_id", [None])[0]
            body = {"data": [{"id": "model-b" if page is None else "model-a", "display_name": "Readable"}]}
            if paginated:
                body.update(has_more=page is None, last_id="model-b" if page is None else "model-a")
            encoded = json.dumps(body if valid else {"error": "fixture header contract mismatch"}).encode()
            self.send_response(200 if valid else 400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/custom/v1", observed
    finally:
        server.shutdown()
        server.server_close()
        worker.join(3)
        assert not worker.is_alive()


CASES = [(protocol, "gateway", auth) for protocol in ("messages", "chat", "responses") for auth in ("bearer", "api_key")]
CASES += [("responses" if CODEX else "messages", "direct", auth) for auth in (["bearer"] if CODEX else ["bearer", "api_key"])]


@pytest.mark.parametrize("protocol,mode,auth", CASES)
def test_selected_protocol_version_and_auth_reach_every_catalog_page(tmp_path, monkeypatch, protocol, mode, auth):
    if mode == "gateway":
        pytest.importorskip("protoverta.discovery")
    prefix = "CODEX_CA" if CODEX else "CLAUDE_CA"
    monkeypatch.setenv(prefix + "_ENV_FILE", str(tmp_path / "unused.env"))
    monkeypatch.setenv(prefix + "_CACHE", str(tmp_path / "cache.json"))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    # Both auth modes are independent of protocol; no key lookup is needed.
    with provider_server(protocol, auth, paginated=protocol == "messages") as (base, observed):
        wire = {"messages": "anthropic_messages", "chat": "chat_completions", "responses": "responses"}[protocol] if CODEX else protocol
        provider = configuration.Provider("fixture", "Fixture", base, protocol=wire, execution_mode=mode, auth_mode=auth)
        if CODEX:
            provider.discovery.enabled = True
        config = configuration.Config(providers={"fixture": provider}, path=tmp_path / "config.toml")
        secret_lookup = "resolve_token" if CODEX else "get_token"
        monkeypatch.setattr(catalog, secret_lookup, lambda *a, **kw: pytest.fail("explicit credential must be reused"))
        entries = []
        result = catalog.fetch_models(config, provider, token="synthetic-key", cache_sink=entries.append)
    identifiers = result if CODEX else [item["id"] for item in result]
    assert sorted(identifiers) == (["model-a", "model-b"] if protocol == "messages" else ["model-b"])
    assert len(observed) == (2 if protocol == "messages" else 1)
    if protocol == "messages":
        assert parse_qs(urlsplit(observed[1][0]).query)["after_id"] == ["model-b"]
    assert len(entries) == 1
    assert "synthetic-key" not in json.dumps(entries)
    assert "anthropic-version" not in json.dumps(entries)  # Credentials/headers are not catalog data.
