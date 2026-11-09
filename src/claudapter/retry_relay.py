"""Loopback relay that retries failed provider requests before Claude Code sees them.

Claude Code retries only timeouts, conflicts, rate limits, server errors and
dropped connections, a few seconds apart. Providers can also fail a request
for a while with other errors and then work again. The relay waits
``delay_seconds`` and sends the same request again, up to ``max_retries``
times, when:

* the provider answers with an HTTP error status not listed in ``skip_status``;
* the provider cannot be reached; or
* a Messages stream sends an ``error`` event or ends before any content.

Once content has been forwarded the stream is passed through unchanged, so
nothing is repeated. When the retries run out, the last error reaches Claude
Code with ``x-should-retry: false`` so it is not retried again on top. Claude
Code answers some errors (HTTP 400) by sending the request again at once with
fewer features enabled; for ``GIVE_UP_WINDOW_SECONDS`` after giving up, failed
requests are passed through without waiting so those follow-ups take seconds,
not another full retry cycle each. The relay
adds no credentials: Claude Code's own headers are forwarded to the configured
provider origin only. Retries are logged to ``retry.log`` beside the model
cache, because Claude Code owns the terminal.
"""

from __future__ import annotations

import http.client
import json
import select
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, BinaryIO, Self
from urllib.parse import urlsplit

from .config import Retry, cache_path

# Headers that describe one connection, not the request, plus Accept-Encoding so
# streams arrive uncompressed and can be inspected.
_HOP_BY_HOP = {
    "accept-encoding",
    "connection",
    "content-encoding",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_PENDING_EVENTS = {"message_start", "ping"}
_FAILED_EVENTS = {"error"}
# Anthropic error types and the HTTP status the API uses for each.
_ERROR_STATUS = {
    "invalid_request_error": 400,
    "authentication_error": 401,
    "permission_error": 403,
    "not_found_error": 404,
    "request_too_large": 413,
    "rate_limit_error": 429,
    "overloaded_error": 529,
}
# Claude Code waits up to 10 minutes for a provider by default; match it.
UPSTREAM_TIMEOUT_SECONDS = 600
GIVE_UP_WINDOW_SECONDS = 30
_CHUNK = 65536


class _ClientGone(Exception):
    """Claude Code closed the connection (for example the user pressed Esc)."""


def _log(message: str) -> None:
    try:
        path = cache_path().parent / "retry.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def _error(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except ValueError:
        return {}
    error = payload.get("error") if isinstance(payload, dict) else None
    return error if isinstance(error, dict) else {}


def _summary(body: bytes) -> str:
    message = _error(body).get("message")
    if isinstance(message, str):
        return message[:200]
    return body[:200].decode("utf-8", "replace")


def _event(block: bytes) -> tuple[str | None, bytes]:
    name = None
    data: list[bytes] = []
    for line in block.splitlines():
        if line.startswith(b"event:"):
            name = line[6:].strip().decode("utf-8", "replace")
        elif line.startswith(b"data:"):
            data.append(line[5:].strip())
    payload = b"\n".join(data)
    if name is None:
        try:
            decoded = json.loads(payload)
        except ValueError:
            decoded = None
        name = decoded.get("type") if isinstance(decoded, dict) else None
    return name, payload


def _read_until_content(response: BinaryIO) -> tuple[bytes, str, bytes]:
    """Buffer a Messages stream until it produces content or fails.

    Returns the buffered bytes, ``"ok"``, ``"failed"`` or ``"ended"`` (closed
    before any content), and the failing event's data.
    """
    buffered = bytearray()
    block = bytearray()
    while True:
        line = response.readline()
        if not line:
            return bytes(buffered), "ended", b""
        buffered += line
        if line.strip():
            block += line
            continue
        if not block:
            continue
        kind, data = _event(bytes(block))
        if kind in _FAILED_EVENTS:
            return bytes(buffered), "failed", data
        if kind not in _PENDING_EVENTS:
            return bytes(buffered), "ok", b""
        block.clear()


class RetryRelay(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, upstream: str, policy: Retry):
        parts = urlsplit(upstream)
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.prefix = parts.path.rstrip("/")
        self.policy = policy
        self.opener = urllib.request.build_opener()
        self.gave_up_at: float | None = None
        super().__init__(("127.0.0.1", 0), _Handler)

    def may_retry(self) -> bool:
        return self.gave_up_at is None or time.monotonic() - self.gave_up_at > GIVE_UP_WINDOW_SECONDS

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}{self.prefix}"

    def __enter__(self) -> Self:
        self._thread = threading.Thread(target=self.serve_forever, name="claudapter-retry-relay", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()
        self.server_close()
        self._thread.join(timeout=5)


class _Handler(BaseHTTPRequestHandler):
    server: RetryRelay
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        self._relay()

    def do_POST(self) -> None:
        self._relay()

    def do_DELETE(self) -> None:
        self._relay()

    def _body(self) -> bytes:
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            body = bytearray()
            while True:
                size = int(self.rfile.readline().split(b";", 1)[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    return bytes(body)
                body += self.rfile.read(size)
                self.rfile.readline()
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _wait(self, attempt: int, reason: str) -> None:
        policy = self.server.policy
        _log(f"{self.command} {self.path}: {reason}; retry {attempt}/{policy.max_retries} in {policy.delay_seconds}s")
        deadline = time.monotonic() + policy.delay_seconds
        while (remaining := deadline - time.monotonic()) > 0:
            readable, _, _ = select.select([self.connection], [], [], min(remaining, 1.0))
            if readable:
                try:
                    if not self.connection.recv(1, socket.MSG_PEEK):
                        raise _ClientGone
                except OSError as exc:
                    raise _ClientGone from exc
                time.sleep(min(remaining, 1.0))

    def _relay(self) -> None:
        self.close_connection = True
        policy = self.server.policy
        body = self._body()
        headers = {name: value for name, value in self.headers.items() if name.lower() not in _HOP_BY_HOP}
        url = self.server.origin + self.path
        attempt = 0
        try:
            while True:
                request = urllib.request.Request(url, data=body if self.command != "GET" else None, headers=headers, method=self.command)
                retryable = attempt < policy.max_retries and self.server.may_retry()
                try:
                    response = self.server.opener.open(request, timeout=UPSTREAM_TIMEOUT_SECONDS)
                except urllib.error.HTTPError as error:
                    payload = error.read()
                    if error.code in policy.skip_status:
                        self._send(error.code, error.headers, payload)
                        return
                    if retryable:
                        attempt += 1
                        self._wait(attempt, f"HTTP {error.code} {_summary(payload)}")
                        continue
                    self._send(error.code, error.headers, payload, final=True)
                    return
                except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
                    if retryable:
                        attempt += 1
                        self._wait(attempt, f"provider unreachable: {error}")
                        continue
                    message = json.dumps({"type": "error", "error": {"type": "api_error", "message": f"Provider unreachable after {attempt} retries: {error}"}}).encode()
                    self._send(502, {"Content-Type": "application/json"}, message, final=True)
                    return
                with response:
                    if "text/event-stream" not in response.headers.get("Content-Type", ""):
                        self.server.gave_up_at = None
                        self._send(response.status, response.headers, response.read())
                        return
                    try:
                        prefix, verdict, data = _read_until_content(response)
                    except (OSError, http.client.HTTPException) as error:
                        prefix, verdict, data = b"", "ended", str(error).encode()
                    if verdict == "ok":
                        self.server.gave_up_at = None
                        self._stream(response, prefix)
                        if attempt:
                            _log(f"{self.command} {self.path}: succeeded after {attempt} retries")
                        return
                    reason = f"stream error: {_summary(data)}" if verdict == "failed" else "stream ended before content"
                    if retryable:
                        attempt += 1
                        self._wait(attempt, reason)
                        continue
                    # Out of retries: report the stream failure as the HTTP
                    # error the API would have sent, marked final.
                    error = _error(data)
                    payload = json.dumps({"type": "error", "error": error or {"type": "api_error", "message": f"Provider {reason}"}}).encode()
                    status = _ERROR_STATUS.get(str(error.get("type")), 500) if error else 502
                    self._send(status, {"Content-Type": "application/json"}, payload, final=True)
                    return
        except _ClientGone:
            _log(f"{self.command} {self.path}: Claude Code cancelled the request; retries stopped")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _headers(self, headers: Any, final: bool = False) -> None:
        for name, value in headers.items():
            if name.lower() not in _HOP_BY_HOP and not (final and name.lower() == "x-should-retry"):
                self.send_header(name, value)

    def _send(self, status: int, headers: Any, payload: bytes, final: bool = False) -> None:
        self.send_response(status)
        self._headers(headers, final)
        if final:
            self.send_header("x-should-retry", "false")
            if self.server.may_retry():
                self.server.gave_up_at = time.monotonic()
                _log(f"{self.command} {self.path}: gave up after {self.server.policy.max_retries} retries; HTTP {status} {_summary(payload)}")
            else:
                _log(f"{self.command} {self.path}: HTTP {status} passed through (retries ran out moments ago)")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def _stream(self, response: http.client.HTTPResponse, prefix: bytes) -> None:
        self.send_response(response.status)
        self._headers(response.headers)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(prefix)
        self.wfile.flush()
        try:
            while chunk := response.read1(_CHUNK):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, http.client.HTTPException):
            pass  # Claude Code sees the truncated stream and reports it.
