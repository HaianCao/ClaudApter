"""Direct HTTP deadlines interrupt trickling responses and clean up watchdogs."""

import importlib
import json
import socket
import socketserver
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

PACKAGE = "claudapter"
catalog = importlib.import_module(PACKAGE + ".catalog")
config_module = importlib.import_module(PACKAGE + ".config")
credentials = importlib.import_module(PACKAGE + ".credentials")
Deadline = importlib.import_module(PACKAGE + ".http_deadline").Deadline
Error = importlib.import_module(PACKAGE + ".errors").ClaudapterError


@contextmanager
def upstream(mode):
    stopped, started = threading.Event(), threading.Event()
    active = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            active.append(self)
            started.set()
            try:
                if mode == "headers":
                    self.connection.sendall(b"HTTP/1.1 200 OK\r\nX-Fixture: ")
                    while not stopped.wait(0.02):
                        self.connection.sendall(b"x")
                elif mode == "pages":
                    if stopped.wait(0.15):
                        return
                    body = json.dumps({"data": [{"id": "m"}], "has_more": True, "last_id": self.path}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(200)
                    self.send_header("Transfer-Encoding" if mode == "chunked" else "Content-Length", "chunked" if mode == "chunked" else "10000")
                    self.end_headers()
                    while not stopped.wait(0.02):
                        self.wfile.write(b"1\r\nx\r\n" if mode == "chunked" else b"x")
                        self.wfile.flush()
            except (OSError, ConnectionError):
                pass
            finally:
                active.remove(self)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", started, active
    finally:
        stopped.set()
        server.shutdown()
        server.server_close()
        worker.join(2)


def fixture_config(tmp_path, monkeypatch, base):
    codex = PACKAGE == "codex_adapter"
    prefix = "CODEX_CA" if codex else "CLAUDE_CA"
    monkeypatch.setenv(prefix + "_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv(prefix + "_CACHE", str(tmp_path / "cache.json"))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    provider = config_module.Provider("fixture", "Fixture", base)
    config = config_module.Config(providers={"fixture": provider}, path=tmp_path / "config.toml")
    if codex:
        provider.discovery.enabled = True
    else:
        credentials.store_token(config, "fixture", "synthetic-key")
    return config, provider


@pytest.mark.parametrize("mode", ["headers", "body", "chunked"])
@pytest.mark.parametrize("execution_mode", ["direct", "gateway"])
def test_total_deadline_stops_trickling_http_without_leaking_timer(tmp_path, monkeypatch, mode, execution_mode):
    with upstream(mode) as (base, _, active):
        config, provider = fixture_config(tmp_path, monkeypatch, base)
        provider.execution_mode = execution_mode
        before = time.monotonic()
        with pytest.raises(Error) as error:
            catalog.fetch_models(config, provider, timeout=0.25)
        assert type(error.value).__name__ in {"DiscoveryError", "ClaudapterError"}
        assert time.monotonic() - before < 1.5
        assert not (tmp_path / "cache.json").exists()
        end = time.monotonic() + 1
        while active and time.monotonic() < end:
            time.sleep(0.01)
        assert not active
        assert not any(t.name == "AdapterHTTPDeadline" for t in threading.enumerate())


@pytest.mark.parametrize("mode", ["direct", "gateway"])
def test_explicit_cancellation_closes_active_response(tmp_path, monkeypatch, mode):
    with upstream("body") as (base, started, active):
        config, provider = fixture_config(tmp_path, monkeypatch, base)
        provider.execution_mode = mode
        budget, errors = Deadline(10), []

        def fetch():
            try:
                with budget:
                    catalog.fetch_models(config, provider, budget=budget)
            except (Error, TimeoutError) as error:
                errors.append(type(error).__name__)

        worker = threading.Thread(target=fetch)
        worker.start()
        try:
            assert started.wait(2)
            budget.cancel()
            worker.join(2)
            assert not worker.is_alive() and errors
            assert not budget.timer.is_alive()
            end = time.monotonic() + 1
            while active and time.monotonic() < end:
                time.sleep(0.01)
            assert not active
        finally:
            budget.cancel()
            worker.join(2)


def test_cancelled_dns_lookup_never_starts_a_late_connection(tmp_path, monkeypatch):
    with upstream("body") as (base, requested, _):
        config, provider = fixture_config(tmp_path, monkeypatch, base)
        monkeypatch.setenv("NO_PROXY", "*")
        monkeypatch.setenv("no_proxy", "*")
        resolved = socket.getaddrinfo("127.0.0.1", base.split(":")[2].split("/")[0], 0, socket.SOCK_STREAM)
        entered, release = threading.Event(), threading.Event()
        budget, errors = Deadline(10), []

        def resolver(*_):
            entered.set()
            assert release.wait(2)
            return resolved

        def fetch():
            try:
                with budget:
                    catalog.fetch_models(config, provider, budget=budget)
            except (Error, TimeoutError) as error:
                errors.append(type(error).__name__)

        monkeypatch.setattr(socket, "getaddrinfo", resolver)
        worker = threading.Thread(target=fetch)
        worker.start()
        try:
            assert entered.wait(2)
            budget.cancel()
            release.set()
            worker.join(2)
            assert not worker.is_alive() and errors
            assert not requested.is_set()
            assert not budget.timer.is_alive()
        finally:
            budget.cancel()
            release.set()
            worker.join(2)


def test_deadline_interrupts_a_stalled_tls_handshake(tmp_path, monkeypatch):
    hello, closed = threading.Event(), threading.Event()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(2)
            try:
                while self.request.recv(4096):
                    hello.set()  # Read ClientHello but never answer it.
            except OSError:
                pass
            finally:
                closed.set()

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    try:
        config, provider = fixture_config(tmp_path, monkeypatch, f"https://127.0.0.1:{server.server_address[1]}/v1")
        before = time.monotonic()
        with pytest.raises(Error):
            catalog.fetch_models(config, provider, timeout=0.25)
        assert time.monotonic() - before < 1.5
        assert hello.is_set() and closed.wait(1)
        assert not any(t.name == "AdapterHTTPDeadline" for t in threading.enumerate())
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)



@pytest.mark.parametrize("execution_mode", ["direct", "gateway"])
def test_claude_pagination_shares_one_total_deadline(tmp_path, monkeypatch, execution_mode):
    with upstream("pages") as (base, _, _):
        config, provider = fixture_config(tmp_path, monkeypatch, base)
        provider.execution_mode = execution_mode
        before = time.monotonic()
        with pytest.raises(Error) as error:
            catalog.fetch_models(config, provider, timeout=0.25)
        assert type(error.value).__name__ == "ClaudapterError"
        assert time.monotonic() - before < 1.5
        assert not (tmp_path / "cache.json").exists()


@pytest.mark.parametrize("shorter", ["policy", "parent"])
def test_gateway_shorter_policy_or_parent_stops_trickling_headers(tmp_path, monkeypatch, shorter):
    with upstream("headers") as (base, _, active):
        config, provider = fixture_config(tmp_path, monkeypatch, base)
        provider.execution_mode = "gateway"
        provider.gateway_runtime["catalog_policy_file"] = "catalog-policy.json"
        (tmp_path / "catalog-policy.json").write_text(json.dumps({
            "schema_version": 1, "base_url": base, "checked_at": "2026-10-29",
            "evidence": "synthetic loopback fixture",
            "policy": {"timeout": 0.25 if shorter == "policy" else 2},
        }), encoding="utf-8")
        parent = Deadline(0.25 if shorter == "parent" else 5)
        try:
            with parent:
                started = time.monotonic()
                with pytest.raises(Error):
                    catalog.fetch_models(config, provider, timeout=2, budget=parent)
                assert time.monotonic() - started < 1.5
                if shorter == "policy":
                    assert parent.timer.is_alive()
        except TimeoutError:
            # The caller-owned context also reports its own expired deadline.
            assert shorter == "parent"
        assert not parent.timer.is_alive()
        assert not (tmp_path / "cache.json").exists()
        end = time.monotonic() + 1
        while active and time.monotonic() < end:
            time.sleep(0.01)
        assert not active
        assert not any(t.name == "AdapterHTTPDeadline" for t in threading.enumerate())
