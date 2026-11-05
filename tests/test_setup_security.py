import io
from email.message import Message
from unittest.mock import Mock, patch

import pytest

from claudapter import web
from claudapter.config import Config
from claudapter.settings_store import SettingsGuard, atomic_bytes


@pytest.fixture
def handler(tmp_path):
    captured = {}

    class Server:
        server_port = 12345

        def serve_forever(self):
            pass

        def server_close(self):
            pass

    def server(address, handler):
        captured["type"] = handler
        return Server()

    with (
        patch.object(web, "ThreadingHTTPServer", side_effect=server),
        patch.object(web.webbrowser, "open"),
        patch.object(web.secrets, "token_urlsafe", return_value="fixture-token"),
    ):
        web.serve(Config(path=tmp_path / "settings.toml"))

    def create(path="/?token=fixture-token", host="127.0.0.1:12345", origin=None, method="GET"):
        value = captured["type"].__new__(captured["type"])
        value.path, value.command, value.request_version = path, method, "HTTP/1.1"
        value.requestline = method + " " + path + " HTTP/1.1"
        value.headers = Message()
        value.headers["Host"] = host
        if origin is not None:
            value.headers["Origin"] = origin
        value.headers["X-Claudapter-Token"] = "fixture-token"
        value.connection = Mock()
        value.wfile, value.rfile = io.BytesIO(), io.BytesIO()
        return value

    return create


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
@pytest.mark.parametrize("host", ["evil.example:12345", "127.0.0.1:9999", "user@127.0.0.1:12345", "127.0.0.1:12345/path", " 127.0.0.1:12345"])
def test_foreign_authority_rejected_before_processing(handler, method, host):
    value = handler(method=method, host=host)
    getattr(value, "do_" + method)()
    assert b"403" in value.wfile.getvalue().split(b"\r\n", 1)[0]
    assert b"fixture-token" not in value.wfile.getvalue()


@pytest.mark.parametrize("origin", ["null", "https://evil.example", "http://127.0.0.1:9999", "http://127.0.0.1:12345/path"])
def test_foreign_or_malformed_origin_rejected(handler, origin):
    value = handler(origin=origin)
    value.do_GET()
    assert b"403" in value.wfile.getvalue().split(b"\r\n", 1)[0]


def test_root_requires_bootstrap_token_even_with_api_header(handler):
    value = handler(path="/")
    value.do_GET()
    assert b"403" in value.wfile.getvalue().split(b"\r\n", 1)[0]


def test_local_page_and_origin_are_accepted(handler):
    value = handler(origin="http://127.0.0.1:12345")
    value.do_GET()
    assert b"200" in value.wfile.getvalue().split(b"\r\n", 1)[0]
    assert b"Referrer-Policy: no-referrer" in value.wfile.getvalue()


def test_duplicate_host_rejected(handler):
    value = handler()
    value.headers["Host"] = "evil.example:12345"
    value.do_GET()
    assert b"403" in value.wfile.getvalue().split(b"\r\n", 1)[0]


def test_stale_setup_snapshot_cannot_overwrite_other_save(tmp_path):
    target = tmp_path / "settings.toml"
    first, second = SettingsGuard(target), SettingsGuard(target)
    with first.edit():
        atomic_bytes(target, b"first")
    with pytest.raises(ValueError, match="another window"), second.edit():
        atomic_bytes(target, b"second")
    assert target.read_bytes() == b"first"
    with first.edit():
        atomic_bytes(target, b"third")


def test_failed_edit_releases_lock(tmp_path):
    guard = SettingsGuard(tmp_path / "settings.toml")
    with pytest.raises(RuntimeError), guard.edit():
        raise RuntimeError("fixture")
    with guard.edit():
        pass
