"""Direct-mode loopback relay that retries failed provider requests.

Shared verbatim by ClaudApter and CodexApter. In gateway mode Protoverta
retries itself (``GatewayConfig.retry_policy``); this relay is only started for
direct mode, so a request is retried at one layer only.

Failures are classified by ``protoverta.error_classes`` exactly as the gateway
does. A transient failure (rate limit, overload, server or network error, a
stream that fails before any output, a backend mismatch, an unknown error) is
resent after the policy's delay; a deterministic one (bad key, no credit,
unknown model, context overflow, content policy, invalid request) reaches the
CLI at once. Output already forwarded is never resent.

The last failure is marked ``x-should-retry: false`` so the CLI does not retry
it on top. For a short window after giving up, further failures pass straight
through: Codex reconnects and Claude Code resends a 400 with fewer features,
and those follow-ups must not run another full cycle each.

The relay adds no credentials: the CLI's own headers go to the configured
provider origin only. Decisions are logged to ``retry.log`` (bounded) because
the CLI owns the terminal.
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
from typing import Any, Self
from urllib.parse import urlsplit

from protoverta.error_classes import RetryPolicy, classify, describe
from protoverta.retry import RetryState, retry_after

from .retry_log import RetryLog

# Connection-scoped headers, plus Accept-Encoding so streams arrive
# uncompressed and can be inspected.
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
_PENDING_EVENTS = {
    "messages": frozenset({"message_start", "ping"}),
    "responses": frozenset({"response.created", "response.in_progress", "response.queued"}),
}
_FAILED_EVENTS = {"messages": frozenset({"error"}), "responses": frozenset({"error", "response.failed"})}
# Anthropic error types and the HTTP status the API uses for each, so a failed
# Messages stream can be answered as the error the API would have sent.
_MESSAGES_ERROR_STATUS = {
    "invalid_request_error": 400,
    "authentication_error": 401,
    "permission_error": 403,
    "not_found_error": 404,
    "request_too_large": 413,
    "rate_limit_error": 429,
    "overloaded_error": 529,
}
# Both CLIs wait about as long for a provider; the relay matches them.
UPSTREAM_TIMEOUT_SECONDS = 600
_CHUNK = 65536


class _ClientGone(Exception):
    """The CLI closed the connection (the user cancelled the turn)."""


def _event(block: bytes) -> tuple[str | None, str]:
    name = None
    data: list[str] = []
    for raw in block.splitlines():
        line = raw.decode("utf-8", "replace")
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())
    payload = "\n".join(data)
    if name is None:
        try:
            decoded = json.loads(payload)
        except ValueError:
            decoded = None
        name = decoded.get("type") if isinstance(decoded, dict) else None
    return name, payload


def _read_until_output(response: Any, protocol: str) -> tuple[bytes, str, str]:
    """Buffer a stream until it produces output, fails or ends.

    Returns the buffered bytes, ``ok``/``failed``/``ended`` and the failing
    event's data.
    """
    buffered = bytearray()
    block = bytearray()
    while True:
        line = response.readline()
        if not line:
            return bytes(buffered), "ended", ""
        buffered += line
        if line.strip():
            block += line
            continue
        if not block:
            continue
        kind, data = _event(bytes(block))
        if kind in _FAILED_EVENTS[protocol]:
            return bytes(buffered), "failed", data
        if kind not in _PENDING_EVENTS[protocol]:
            return bytes(buffered), "ok", ""
        block.clear()


class RetryRelay(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, upstream: str, policy: RetryPolicy, protocol: str, log: RetryLog | None = None) -> None:
        if protocol not in _PENDING_EVENTS:
            raise ValueError("relay protocol must be messages or responses")
        parts = urlsplit(upstream)
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.prefix = parts.path.rstrip("/")
        self.policy = policy
        self.protocol = protocol
        self.log = log or (lambda message: None)
        self.state = RetryState()
        self.opener = urllib.request.build_opener()
        super().__init__(("127.0.0.1", 0), _Handler)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}{self.prefix}"

    def __enter__(self) -> Self:
        self._thread = threading.Thread(target=self.serve_forever, name="adapter-retry-relay", daemon=True)
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

    def _wait(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
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
        server = self.server
        policy = server.policy
        body = self._body()
        headers = {name: value for name, value in self.headers.items() if name.lower() not in _HOP_BY_HOP}
        url = server.origin + self.path
        attempt = 0
        waited = 0.0
        where = f"{self.command} {urlsplit(self.path).path}"

        def retry(error_class: Any, status: int | None, upstream_headers: Any, reason: str) -> bool:
            nonlocal attempt, waited
            if attempt >= policy.max_retries or not policy.retryable(error_class, status, upstream_headers):
                return False
            if not server.state.may_retry(policy.give_up_window_seconds):
                return False
            delay = policy.delay(retry_after(upstream_headers) if upstream_headers is not None else None)
            if waited + delay > policy.max_total_seconds:
                return False
            attempt += 1
            server.log(f"{where}: {reason}; retry {attempt}/{policy.max_retries} in {delay:.0f}s")
            self._wait(delay)
            waited += delay
            return True

        def give_up(reason: str) -> None:
            if not server.state.may_retry(policy.give_up_window_seconds):
                server.log(f"{where}: {reason}; passed through (retries ran out moments ago)")
                return
            server.log(
                f"{where}: gave up after {attempt} retries: {reason}" if attempt else f"{where}: not retried: {reason}"
            )
            if attempt:
                server.state.give_up()

        def succeeded() -> None:
            server.state.succeeded()
            if attempt:
                server.log(f"{where}: succeeded after {attempt} retries")

        try:
            while True:
                request = urllib.request.Request(
                    url, data=body if self.command != "GET" else None, headers=headers, method=self.command
                )
                try:
                    response = server.opener.open(request, timeout=UPSTREAM_TIMEOUT_SECONDS)
                except urllib.error.HTTPError as error:
                    payload = error.read()
                    error_class = classify(error.code, payload)
                    reason = describe(error_class, error.code, payload)
                    if retry(error_class, error.code, error.headers, reason):
                        continue
                    give_up(reason)
                    self._send(error.code, error.headers, payload, final=True)
                    return
                except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
                    reason = f"network ({type(error).__name__})"
                    if retry("network", None, None, reason):
                        continue
                    give_up(reason)
                    message = {
                        "type": "error",
                        "error": {
                            "type": "api_error",
                            "message": f"Provider unreachable after {attempt} retries: {error}",
                        },
                    }
                    self._send(502, {"Content-Type": "application/json"}, json.dumps(message).encode(), final=True)
                    return
                with response:
                    if "text/event-stream" not in response.headers.get("Content-Type", ""):
                        succeeded()
                        self._send(response.status, response.headers, response.read())
                        return
                    try:
                        prefix, verdict, data = _read_until_output(response, server.protocol)
                    except (OSError, http.client.HTTPException) as error:
                        prefix, verdict, data = b"", "ended", str(error)
                    if verdict == "ok":
                        succeeded()
                        self._stream(response, prefix)
                        return
                    error_class = (
                        classify(None, data, stream_before_output=True) if verdict == "failed" else "stream_interrupted"
                    )
                    reason = (
                        describe(error_class, None, data)
                        if verdict == "failed"
                        else "stream_interrupted (stream ended before output)"
                    )
                    if retry(error_class, None, None, reason):
                        continue
                    give_up(reason)
                    self._final_stream_failure(prefix, verdict, data)
                    return
        except _ClientGone:
            server.log(f"{where}: the CLI cancelled the request; retries stopped")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _final_stream_failure(self, prefix: bytes, verdict: str, data: str) -> None:
        if self.server.protocol == "responses":
            # Codex reports a forwarded response.failed event itself.
            self._send(200, {"Content-Type": "text/event-stream"}, prefix, final=True)
            return
        # A Messages client expects an HTTP error for a failure before output.
        try:
            detail = json.loads(data).get("error") if verdict == "failed" else None
        except (ValueError, AttributeError):
            detail = None
        if not isinstance(detail, dict):
            detail = {"type": "api_error", "message": "Provider stream ended before any content"}
        status = _MESSAGES_ERROR_STATUS.get(str(detail.get("type")), 500 if verdict == "failed" else 502)
        self._send(
            status,
            {"Content-Type": "application/json"},
            json.dumps({"type": "error", "error": detail}).encode(),
            final=True,
        )

    def _headers(self, headers: Any, final: bool) -> None:
        for name, value in headers.items():
            if name.lower() not in _HOP_BY_HOP and not (final and name.lower() == "x-should-retry"):
                self.send_header(name, value)
        if final:
            self.send_header("x-should-retry", "false")

    def _send(self, status: int, headers: Any, payload: bytes, *, final: bool = False) -> None:
        self.send_response(status)
        self._headers(headers, final)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def _stream(self, response: http.client.HTTPResponse, prefix: bytes) -> None:
        self.send_response(response.status)
        self._headers(response.headers, final=False)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(prefix)
        self.wfile.flush()
        try:
            while chunk := response.read1(_CHUNK):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, http.client.HTTPException):
            pass  # The CLI sees the truncated stream and reports it.
