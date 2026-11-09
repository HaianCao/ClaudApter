"""Loopback-only tests for the retry relay; no credentials or provider calls."""

import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from claudapter.config import Config, Provider, Retry, load, save
from claudapter.credentials import store_token
from claudapter.errors import ClaudapterError
from claudapter.launcher import build_invocation
from claudapter.retry_relay import RetryRelay

BAD = b'{"type":"error","error":{"type":"invalid_request_error","message":"Upstream backend rejected the request."}}'


def sse(*events):
    return b"".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode() for event in events)


START = {"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant", "content": [], "model": "x"}}
PING = {"type": "ping"}
BLOCK = {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
DELTA = {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "PONG"}}
STOP = {"type": "message_stop"}
OVERLOADED = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
OK_STREAM = sse(START, PING, BLOCK, DELTA, STOP)


class Upstream:
    """Answers each request with the next scripted (status, content type, body, headers)."""

    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                upstream.requests.append((self.path, dict(self.headers), body))
                status, kind, payload, *extra = upstream.replies.pop(0) if len(upstream.replies) > 1 else upstream.replies[0]
                self.send_response(status)
                self.send_header("Content-Type", kind)
                for name, value in (extra[0] if extra else {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/api"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def call(url, body=b'{"model":"m","stream":true}'):
    request = urllib.request.Request(url + "/v1/messages?beta=true", data=body, headers={"Authorization": "Bearer local-key", "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers


class RetryRelayTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.log = Path(directory.name) / "retry.log"
        cache = patch.dict("os.environ", {"CLAUDE_CA_CACHE": str(Path(directory.name) / "models.json")})
        cache.start()
        self.addCleanup(cache.stop)

    def relay(self, upstream, **policy):
        relay = RetryRelay(upstream.url, Retry(**{"delay_seconds": 0, **policy}))
        relay.__enter__()
        self.addCleanup(relay.__exit__, None, None, None)
        self.addCleanup(upstream.close)
        return relay.base_url

    def test_http_error_is_retried_until_the_provider_answers(self):
        upstream = Upstream((400, "application/json", BAD), (500, "application/json", BAD), (200, "text/event-stream", OK_STREAM))
        status, body, _ = call(self.relay(upstream))
        self.assertEqual((status, body), (200, OK_STREAM))
        self.assertEqual(len(upstream.requests), 3)
        path, headers, sent = upstream.requests[-1]
        self.assertEqual(path, "/api/v1/messages?beta=true")
        self.assertEqual(headers["Authorization"], "Bearer local-key")
        self.assertEqual(sent, b'{"model":"m","stream":true}')
        log = self.log.read_text(encoding="utf-8")
        self.assertIn("HTTP 400 Upstream backend rejected the request.; retry 1/10", log)
        self.assertIn("succeeded after 2 retries", log)

    def test_last_error_is_marked_final_when_retries_run_out(self):
        upstream = Upstream((500, "application/json", BAD, {"x-should-retry": "true"}))
        status, body, headers = call(self.relay(upstream, max_retries=3))
        self.assertEqual((status, body), (500, BAD))
        self.assertEqual(headers.get_all("x-should-retry"), ["false"])
        self.assertEqual(len(upstream.requests), 4)

    def test_follow_ups_right_after_giving_up_are_not_retried_again(self):
        upstream = Upstream((400, "application/json", BAD), (400, "application/json", BAD), (400, "application/json", BAD), (200, "text/event-stream", OK_STREAM), (400, "application/json", BAD), (200, "text/event-stream", OK_STREAM))
        url = self.relay(upstream, max_retries=1)
        self.assertEqual(call(url)[0], 400)  # Two attempts, then gives up.
        status, _, headers = call(url)  # Claude Code's immediate follow-up.
        self.assertEqual((status, headers.get("x-should-retry")), (400, "false"))
        self.assertEqual(len(upstream.requests), 3)
        self.assertEqual(call(url)[0], 200)  # Success ends the pass-through window.
        self.assertEqual(call(url)[:2], (200, OK_STREAM))  # Retried again: 400 then OK.
        self.assertEqual(len(upstream.requests), 6)
        self.assertIn("passed through (retries ran out moments ago)", self.log.read_text(encoding="utf-8"))

    def test_skipped_status_is_passed_through_unchanged(self):
        upstream = Upstream((401, "application/json", b'{"type":"error","error":{"type":"authentication_error","message":"bad key"}}'))
        status, _, headers = call(self.relay(upstream))
        self.assertEqual(status, 401)
        self.assertIsNone(headers.get("x-should-retry"))
        self.assertEqual(len(upstream.requests), 1)

    def test_stream_failing_before_content_is_retried(self):
        upstream = Upstream((200, "text/event-stream", sse(START, PING, OVERLOADED)), (200, "text/event-stream", b""), (200, "text/event-stream", OK_STREAM))
        self.assertEqual(call(self.relay(upstream))[:2], (200, OK_STREAM))
        self.assertEqual(len(upstream.requests), 3)
        log = self.log.read_text(encoding="utf-8")
        self.assertIn("stream error: Overloaded", log)
        self.assertIn("stream ended before content", log)

    def test_stream_failing_after_content_is_passed_through(self):
        partial = sse(START, BLOCK, DELTA, OVERLOADED)
        upstream = Upstream((200, "text/event-stream", partial), (200, "text/event-stream", OK_STREAM))
        self.assertEqual(call(self.relay(upstream))[:2], (200, partial))
        self.assertEqual(len(upstream.requests), 1)

    def test_failed_stream_becomes_a_final_http_error_when_retries_run_out(self):
        upstream = Upstream((200, "text/event-stream", sse(START, OVERLOADED)))
        status, body, headers = call(self.relay(upstream, max_retries=1))
        self.assertEqual(status, 529)
        self.assertEqual(json.loads(body), {"type": "error", "error": OVERLOADED["error"]})
        self.assertEqual(headers.get("x-should-retry"), "false")
        self.assertEqual(len(upstream.requests), 2)

    def test_unreachable_provider_returns_final_502_after_retries(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with RetryRelay(f"http://127.0.0.1:{port}/api", Retry(max_retries=2, delay_seconds=0)) as relay:
            status, body, headers = call(relay.base_url)
        self.assertEqual(status, 502)
        self.assertIn("unreachable after 2 retries", json.loads(body)["error"]["message"])
        self.assertEqual(headers.get("x-should-retry"), "false")

    def test_cancelled_request_stops_waiting(self):
        upstream = Upstream((400, "application/json", BAD))
        url = self.relay(upstream, delay_seconds=30)
        port = int(url.split(":")[2].split("/")[0])
        body = b'{"model":"m"}'
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.sendall(b"POST /api/v1/messages HTTP/1.1\r\nHost: x\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            deadline = time.monotonic() + 5
            while not upstream.requests and time.monotonic() < deadline:
                time.sleep(0.05)
        deadline = time.monotonic() + 5
        while "cancelled" not in (self.log.read_text(encoding="utf-8") if self.log.exists() else "") and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIn("Claude Code cancelled the request", self.log.read_text(encoding="utf-8"))
        self.assertEqual(len(upstream.requests), 1)


class RetryConfigTests(unittest.TestCase):
    def test_defaults_round_trip_and_custom_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            save(Config(path=path))
            self.assertEqual(load(path).retry, Retry())
            path.write_text("schema_version = 1\n\n[retry]\nenabled = true\nmax_retries = 3\ndelay_seconds = 5\nskip_status = [401]\n", encoding="utf-8")
            loaded = load(path)
            self.assertEqual(loaded.retry, Retry(True, 3, 5, (401,)))
            save(loaded)
            self.assertEqual(load(path).retry, Retry(True, 3, 5, (401,)))

    def test_invalid_values_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            for table in ("max_retries = -1", "max_retries = 101", "delay_seconds = 3601", "enabled = 1", 'skip_status = "401"', "skip_status = [200]"):
                path.write_text(f"schema_version = 1\n\n[retry]\n{table}\n", encoding="utf-8")
                with self.subTest(table=table), self.assertRaises(ClaudapterError):
                    load(path)

    def test_claude_code_points_at_the_relay_with_a_longer_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = Provider("demo", "Demo", "https://gateway.test/v1")
            config = Config({"demo": provider}, path=Path(directory) / "config.toml")
            store_token(config, "demo", "secret")
            with patch("claudapter.launcher.shutil.which", return_value="claude.exe"), patch.dict("os.environ", {}, clear=False) as environ:
                environ.pop("API_TIMEOUT_MS", None)
                _, relayed = build_invocation(config, provider, "m", "default", [], relay_url="http://127.0.0.1:5000")
                _, plain = build_invocation(config, provider, "m", "default", [])
                config.retry = Retry(max_retries=100, delay_seconds=3600)
                _, capped = build_invocation(config, provider, "m", "default", [], relay_url="http://127.0.0.1:5000")
                environ["API_TIMEOUT_MS"] = "900000"
                _, explicit = build_invocation(config, provider, "m", "default", [], relay_url="http://127.0.0.1:5000")
        self.assertEqual(relayed["ANTHROPIC_BASE_URL"], "http://127.0.0.1:5000")
        self.assertEqual(relayed["ANTHROPIC_AUTH_TOKEN"], "secret")
        self.assertEqual(relayed["API_TIMEOUT_MS"], "1200000")
        self.assertEqual(plain["ANTHROPIC_BASE_URL"], "https://gateway.test")
        self.assertNotIn("API_TIMEOUT_MS", plain)
        self.assertEqual(capped["API_TIMEOUT_MS"], "1800000")
        self.assertEqual(explicit["API_TIMEOUT_MS"], "900000")


if __name__ == "__main__":
    unittest.main()
