"""Actual setup listener regressions; synthetic configuration and no provider IO."""

import queue
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
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
