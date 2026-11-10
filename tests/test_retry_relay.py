"""Loopback-only tests for the direct-mode retry relay and [retry] config."""

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

from protoverta.error_classes import RetryPolicy

from claudapter.config import Config, Provider, load, retry_policy, save
from claudapter.credentials import store_token
from claudapter.errors import ClaudapterError
from claudapter.launcher import build_invocation
from claudapter.retry_log import LOG_LIMIT_BYTES, RetryLog
from claudapter.retry_relay import RetryRelay

AZURE = b'{"type":"error","error":{"type":"invalid_request_error","message":"The requested item was created under a different Azure OpenAI resource."}}'
BAD_MODEL = b'{"type":"error","error":{"type":"invalid_request_error","message":"vendor/x is not a valid model ID"}}'
BUSY = b'{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'


def sse(*events):
    return b"".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode() for event in events)


START = {"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant", "content": [], "model": "x"}}
BLOCK = {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
DELTA = {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "PONG"}}
STOP = {"type": "message_stop"}
OVERLOADED = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
OK_STREAM = sse(START, BLOCK, DELTA, STOP)


class Upstream:
    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                upstream.requests.append((self.path, dict(self.headers), body))
                status, kind, payload = upstream.replies.pop(0) if len(upstream.replies) > 1 else upstream.replies[0]
                self.send_response(status)
                self.send_header("Content-Type", kind)
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
    request = urllib.request.Request(
        url + "/v1/messages?beta=true", data=body, headers={"Authorization": "Bearer key", "Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers


class RelayTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.log_path = Path(directory.name) / "retry.log"

    def relay(self, upstream, **policy):
        relay = RetryRelay(upstream.url, RetryPolicy(**{"delay_seconds": 0, "jitter": 0, **policy}), "messages", RetryLog(self.log_path))
        relay.__enter__()
        self.addCleanup(relay.__exit__, None, None, None)
        self.addCleanup(upstream.close)
        return relay.base_url

    def log(self):
        return self.log_path.read_text(encoding="utf-8") if self.log_path.exists() else ""

    def test_transient_errors_are_retried_and_classified_in_the_log(self):
        upstream = Upstream((400, "application/json", AZURE), (529, "application/json", BUSY), (200, "text/event-stream", OK_STREAM))
        status, body, _ = call(self.relay(upstream))
        self.assertEqual((status, body), (200, OK_STREAM))
        self.assertEqual(len(upstream.requests), 3)
        path, headers, _sent = upstream.requests[-1]
        self.assertEqual(path, "/api/v1/messages?beta=true")
        self.assertEqual(headers["Authorization"], "Bearer key")
        self.assertIn("backend_mismatch (HTTP 400)", self.log())
        self.assertIn("overloaded (HTTP 529)", self.log())
        self.assertIn("succeeded after 2 retries", self.log())

    def test_deterministic_error_is_not_retried_and_marked_final(self):
        upstream = Upstream((400, "application/json", BAD_MODEL), (200, "text/event-stream", OK_STREAM))
        status, _body, headers = call(self.relay(upstream))
        self.assertEqual((status, len(upstream.requests)), (400, 1))
        self.assertEqual(headers.get("x-should-retry"), "false")
        self.assertIn("not retried: model_not_found", self.log())

    def test_exhausted_retries_and_give_up_window(self):
        upstream = Upstream((503, "application/json", BUSY))
        url = self.relay(upstream, max_retries=2)
        self.assertEqual(call(url)[0], 503)
        self.assertEqual(len(upstream.requests), 3)
        self.assertEqual(call(url)[0], 503)  # The CLI's immediate follow-up.
        self.assertEqual(len(upstream.requests), 4)
        self.assertIn("gave up after 2 retries", self.log())
        self.assertIn("passed through", self.log())

    def test_stream_failing_before_content_is_retried_and_after_content_passed_through(self):
        upstream = Upstream((200, "text/event-stream", sse(START, OVERLOADED)), (200, "text/event-stream", b""), (200, "text/event-stream", OK_STREAM))
        self.assertEqual(call(self.relay(upstream))[:2], (200, OK_STREAM))
        self.assertEqual(len(upstream.requests), 3)
        partial = sse(START, BLOCK, DELTA, OVERLOADED)
        upstream = Upstream((200, "text/event-stream", partial), (200, "text/event-stream", OK_STREAM))
        self.assertEqual(call(self.relay(upstream))[:2], (200, partial))
        self.assertEqual(len(upstream.requests), 1)

    def test_failed_stream_becomes_the_matching_http_error_when_retries_run_out(self):
        upstream = Upstream((200, "text/event-stream", sse(START, OVERLOADED)))
        status, body, headers = call(self.relay(upstream, max_retries=1))
        self.assertEqual(status, 529)
        self.assertEqual(json.loads(body)["error"]["type"], "overloaded_error")
        self.assertEqual(headers.get("x-should-retry"), "false")

    def test_unreachable_provider(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with RetryRelay(f"http://127.0.0.1:{port}/api", RetryPolicy(max_retries=2, delay_seconds=0, jitter=0), "messages") as relay:
            status, body, _headers = call(relay.base_url)
        self.assertEqual(status, 502)
        self.assertIn("unreachable after 2 retries", json.loads(body)["error"]["message"])

    def test_cancelled_request_stops_waiting(self):
        upstream = Upstream((503, "application/json", BUSY))
        url = self.relay(upstream, delay_seconds=30)
        port = int(url.split(":")[2].split("/")[0])
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.sendall(b"POST /api/v1/messages HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}")
            deadline = time.monotonic() + 5
            while not upstream.requests and time.monotonic() < deadline:
                time.sleep(0.05)
        deadline = time.monotonic() + 5
        while "cancelled" not in self.log() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIn("cancelled the request", self.log())
        self.assertEqual(len(upstream.requests), 1)

    def test_log_is_bounded(self):
        log = RetryLog(self.log_path)
        self.log_path.write_text("x" * (LOG_LIMIT_BYTES + 1), encoding="utf-8")
        log("next")
        self.assertTrue(self.log_path.with_name("retry.log.1").exists())
        self.assertLess(self.log_path.stat().st_size, 100)


class RetryConfigTests(unittest.TestCase):
    def test_absent_table_uses_defaults_and_is_not_written(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            save(Config(path=path))
            self.assertNotIn("[retry]", path.read_text(encoding="utf-8"))
            self.assertEqual(retry_policy(load(path)), RetryPolicy())

    def test_table_written_by_0_1_x_round_trips(self):
        # ClaudApter 0.1.1 wrote exactly this table.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text("schema_version = 1\n\n[retry]\nenabled = true\nmax_retries = 3\ndelay_seconds = 5\nskip_status = [401, 403]\n", encoding="utf-8")
            config = load(path)
            save(config)
            self.assertEqual(load(path).retry, {"enabled": True, "max_retries": 3, "delay_seconds": 5, "skip_status": [401, 403]})
            policy = retry_policy(load(path))
            self.assertEqual((policy.max_retries, policy.delay_seconds, policy.skip_status), (3, 5, frozenset({401, 403})))

    def test_invalid_values_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            for table in ("max_retries = -1", "delay_seconds = 3601", "enabled = 1", 'retry_classes = ["nope"]', "surprise = 1"):
                path.write_text(f"schema_version = 1\n\n[retry]\n{table}\n", encoding="utf-8")
                with self.subTest(table=table), self.assertRaises(ClaudapterError):
                    load(path)

    def test_claude_code_gets_a_longer_timeout_through_the_relay(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = Provider("demo", "Demo", "https://gateway.test/v1")
            config = Config({"demo": provider}, path=Path(directory) / "config.toml")
            store_token(config, "demo", "secret")
            with patch("claudapter.launcher.shutil.which", return_value="claude.exe"), patch.dict("os.environ", {}, clear=False) as environ:
                environ.pop("API_TIMEOUT_MS", None)
                _, relayed = build_invocation(config, provider, "m", "default", [], relay_url="http://127.0.0.1:5000")
                _, plain = build_invocation(config, provider, "m", "default", [])
        self.assertEqual(relayed["ANTHROPIC_BASE_URL"], "http://127.0.0.1:5000")
        self.assertEqual(relayed["API_TIMEOUT_MS"], "1320000")  # 10 x 60 s x 1.2 + 600 s.
        self.assertEqual(plain["ANTHROPIC_BASE_URL"], "https://gateway.test")
        self.assertNotIn("API_TIMEOUT_MS", plain)


if __name__ == "__main__":
    unittest.main()
