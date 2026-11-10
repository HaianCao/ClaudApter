"""Actual setup listener regressions; synthetic configuration and no provider IO."""

import json
import queue
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, nullcontext
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import Request, urlopen

import pytest

from claudapter import web
from claudapter.config import Config
from claudapter.web_http import SetupHTTPServer
from claudapter.web_security import token_matches


@contextmanager
def listener(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CA_ENV_FILE", str(tmp_path / ".env"))
    urls, servers, errors = queue.Queue(), [], []

    class Server(SetupHTTPServer):
        def __init__(self, address, handler):
            handler.header_timeout = 0.6
            handler.body_timeout = 0.3
            handler.input_timeout = 1.0
            super().__init__(address, handler, max_connections=2)
            servers.append(self)

        def handle_error(self, *_):
            errors.append("unexpected handler exception")

    config = Config(path=tmp_path / "config.toml")
    with patch.object(web, "ThreadingHTTPServer", Server), patch.object(web.webbrowser, "open", side_effect=urls.put):
        worker = threading.Thread(target=lambda: web.serve(config), daemon=True)
        worker.start()
        parsed = urlsplit(urls.get(timeout=3))
        token = parse_qs(parsed.query)["token"][0]
        try:
            yield servers[0], parsed, token, errors
        finally:
            servers[0].shutdown()
            servers[0].server_close()
            worker.join(3)
        assert not worker.is_alive() and not errors


def test_non_ascii_query_and_header_are_unauthorized_not_handler_errors(tmp_path, monkeypatch):
    with listener(tmp_path, monkeypatch) as (_, url, token, _):
        for request in (
            Request(f"http://{url.netloc}/?token=" + quote("é💡")),
            Request(f"http://{url.netloc}/api/config", headers={"X-Claudapter-Token": "é"}),
        ):
            with pytest.raises(HTTPError) as error:
                urlopen(request, timeout=2)
            with error.value as response:
                assert response.code == 403
        with urlopen(Request(f"http://{url.netloc}/api/config", headers={"X-Claudapter-Token": token}), timeout=2) as response:
            assert response.status == 200


def test_numeric_provider_save_and_reload_keeps_setup_and_other_key_readable(tmp_path, monkeypatch):
    from claudapter.config import load, secret_var
    from claudapter.credentials import get_token

    monkeypatch.delenv(secret_var("123"), raising=False)
    monkeypatch.delenv(secret_var("other"), raising=False)
    with listener(tmp_path, monkeypatch) as (_, url, token, _):
        origin = f"http://{url.netloc}"
        headers = {"X-Claudapter-Token": token, "Content-Type": "application/json"}
        for provider_id in ("123", "other"):
            body = {"id": provider_id, "display_name": "Fixture " + provider_id, "base_url": "https://example.test/v1", "api_key": "synthetic-" + provider_id}
            with urlopen(Request(origin + "/api/provider", data=json.dumps(body).encode(), headers=headers), timeout=3) as response:
                assert response.status == 200
        with urlopen(Request(origin + "/api/config", headers=headers), timeout=3) as response:
            assert {p["id"]: p["has_key"] for p in json.load(response)["providers"]} == {"123": True, "other": True}
        config = load(tmp_path / "config.toml")
        assert get_token(config, "123") == "synthetic-123"
        assert get_token(config, "other") == "synthetic-other"

    # Reconstruct setup's public state from the saved configuration.
    assert {p["id"]: p["has_key"] for p in web.public_config(load(tmp_path / "config.toml"))["providers"]} == {"123": True, "other": True}


def test_invalid_key_save_is_atomic_and_existing_bad_key_can_be_replaced(tmp_path, monkeypatch):
    from claudapter import launcher

    monkeypatch.setattr(launcher.shutil, "which", lambda _: "claude.exe")
    with listener(tmp_path, monkeypatch) as (_, url, token, _):
        origin = f"http://{url.netloc}"
        headers = {"X-Claudapter-Token": token, "Content-Type": "application/json"}

        def post(path, body):
            return urlopen(Request(origin + path, data=json.dumps(body).encode(), headers=headers), timeout=3)

        body = {"id": "fixture", "display_name": "Fixture", "base_url": "https://example.test/v1", "api_key": "synthetic-valid"}
        with post("/api/provider", body) as response:
            assert response.status == 200
        files = [tmp_path / "config.toml", tmp_path / ".env"]
        before = [path.read_bytes() for path in files]
        with pytest.raises(HTTPError) as error:
            post("/api/provider", {**body, "display_name": "Must not save", "api_key": "synthetic\0invalid"})
        with error.value as response:
            assert response.code == 400
            assert json.load(response) == {"error": "API key cannot be empty or contain control characters."}
        assert [path.read_bytes() for path in files] == before

        # Simulate a key saved by an older version, without changing config.
        files[1].write_text('FIXTURE_TOKEN="synthetic\\u0000invalid"\n')
        with urlopen(Request(origin + "/api/config", headers=headers), timeout=3) as response:
            assert response.status == 200
        with patch.object(web, "launch") as launched, pytest.raises(HTTPError) as error:
            post("/api/run", {"provider": "fixture", "model": "m", "effort": "default"})
        with error.value as response:
            assert response.code == 400
            assert "control characters" in json.load(response)["error"]
        launched.assert_not_called()
        assert files[0].read_bytes() == before[0]
        with post("/api/provider", body) as response:
            assert response.status == 200


def test_missing_mcp_variable_keeps_setup_open_and_does_not_save_defaults(tmp_path, monkeypatch):
    from claudapter import agent_models, launcher

    # Newly added setup providers now select an explicit agent model policy.
    # Keep this fake executable fixture verified so it reaches MCP validation.
    monkeypatch.setattr(agent_models, "detect_version", lambda _: (2, 1, 308))

    mcp = tmp_path / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {"fixture": {"command": sys.executable, "env_vars": ["MISSING_FIXTURE_VARIABLE"]}}}))
    monkeypatch.setenv("CLAUDE_CA_MCP_CONFIG", str(mcp))
    monkeypatch.delenv("MISSING_FIXTURE_VARIABLE", raising=False)
    monkeypatch.setattr(launcher.shutil, "which", lambda _: "claude.exe")
    monkeypatch.setattr(launcher, "get_token", lambda *_: "synthetic")
    with listener(tmp_path, monkeypatch) as (_, url, token, _), patch.object(web, "launch") as launched:
        origin = f"http://{url.netloc}"
        headers = {"X-Claudapter-Token": token, "Content-Type": "application/json"}
        provider = {"id": "fixture", "display_name": "Fixture", "base_url": "https://example.test/v1", "api_key": "synthetic"}
        with urlopen(Request(origin + "/api/provider", data=json.dumps(provider).encode(), headers=headers), timeout=3) as response:
            assert response.status == 200
        before = (tmp_path / "config.toml").read_bytes()
        with pytest.raises(HTTPError) as error:
            urlopen(Request(origin + "/api/run", data=json.dumps({"provider": "fixture", "model": "m"}).encode(), headers=headers), timeout=3)
        with error.value as response:
            assert response.status == 400
            assert "missing from the child" in json.load(response)["error"]
        with urlopen(Request(origin + "/api/config", headers=headers), timeout=3) as response:
            state = json.load(response)
            assert state["defaults"]["provider"] == ""
        assert (tmp_path / "config.toml").read_bytes() == before
        launched.assert_not_called()


@pytest.mark.parametrize("value", ["é", "💡", "", None, b"fixture"])
def test_token_matcher_rejects_malformed_values(value):
    assert token_matches(value, "fixture") is False


def test_header_deadline_capacity_and_recovery(tmp_path, monkeypatch):
    with listener(tmp_path, monkeypatch) as (server, url, token, _):
        clients = []
        try:
            for _ in range(2):
                client = socket.create_connection(server.server_address, timeout=2)
                client.sendall(b"GET / HTTP/1.0\r\nX-Fixture:")
                clients.append(client)
            with socket.create_connection(server.server_address, timeout=2) as extra:
                extra.sendall(b"GET / HTTP/1.0\r\n\r\n")
                try:
                    assert extra.recv(4096) == b""
                except ConnectionError:
                    pass
            for client in clients:
                try:
                    assert client.recv(4096) == b""
                except ConnectionError:
                    pass
            deadline = time.monotonic() + 2
            while server._active and time.monotonic() < deadline:
                time.sleep(0.01)
            assert not server._active
            with urlopen(Request(f"http://{url.netloc}/api/config", headers={"X-Claudapter-Token": token}), timeout=2) as response:
                assert response.status == 200
        finally:
            for client in clients:
                client.close()


def test_trickling_body_has_absolute_deadline(tmp_path, monkeypatch):
    with listener(tmp_path, monkeypatch) as (server, url, token, _), socket.create_connection(server.server_address, timeout=2) as client:
        headers = f"POST /api/provider HTTP/1.0\r\nHost: {url.netloc}\r\nX-Claudapter-Token: {token}\r\nContent-Length: 1000\r\n\r\n".encode()
        client.sendall(headers + b"{")
        for _ in range(2):
            time.sleep(0.09)
            client.sendall(b" ")
        response = client.recv(4096)
        assert b"408" in response.split(b"\r\n", 1)[0]


def test_shutdown_closes_pending_header_clients(tmp_path, monkeypatch):
    with listener(tmp_path, monkeypatch) as (server, _, _, _), socket.create_connection(server.server_address, timeout=2) as client:
        client.sendall(b"GET / HTTP/1.0\r\nX-Fixture:")
        server.shutdown()
        server.server_close()
        try:
            assert client.recv(4096) == b""
        except ConnectionError:
            pass


def test_setup_import_does_not_require_protoverta():
    code = """
import importlib.abc,sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname == 'protoverta' or fullname.startswith('protoverta.'):
            raise ImportError('optional core blocked for direct-only test')
sys.meta_path.insert(0,Block())
import claudapter.web
"""
    subprocess.run([sys.executable, "-c", code], check=True, timeout=15)


@pytest.mark.parametrize("force_decoder_error", [False, True])
def test_deep_json_returns_400_without_mutation_and_valid_post_recovers(tmp_path, monkeypatch, force_decoder_error):
    with listener(tmp_path, monkeypatch) as (_, url, token, _):
        headers = {"X-Claudapter-Token": token, "Content-Type": "application/json"}

        def state():
            with urlopen(Request(f"http://{url.netloc}/api/config", headers=headers), timeout=2) as response:
                return json.load(response)

        def files():
            return {name: (tmp_path / name).read_bytes() if (tmp_path / name).exists() else None for name in ("config.toml", ".env")}

        before, saved = state(), files()
        # Valid JSON below the body byte cap, but beyond the decoder's nesting.
        data = b'{"x":' + b"[" * 4000 + b"0" + b"]" * 4000 + b"}"
        assert len(data) < 64000
        try:
            json.loads(data)
            actual_overflow = False
        except RecursionError:
            actual_overflow = True
        # Decoder stack limits vary by OS/Python. Also inject the exception so
        # this boundary contract is exercised even where this payload parses.
        decoder = patch.object(web.json, "loads", side_effect=RecursionError("private decoder detail")) if force_decoder_error else nullcontext()
        with decoder, pytest.raises(HTTPError) as error:
            urlopen(Request(f"http://{url.netloc}/api/provider", data=data, headers=headers), timeout=2)
        with error.value as response:
            assert response.code == 400
            result = json.load(response)
            if force_decoder_error or actual_overflow:
                assert result == {"error": "JSON request nesting is too deep."}
        assert state() == before and files() == saved

        valid = {"id": "fixture", "display_name": "Fixture", "base_url": "https://example.invalid/v1"}
        with urlopen(Request(f"http://{url.netloc}/api/provider", data=json.dumps(valid).encode(), headers=headers), timeout=2) as response:
            assert response.status == 200
        assert [provider["id"] for provider in state()["providers"]] == ["fixture"]
