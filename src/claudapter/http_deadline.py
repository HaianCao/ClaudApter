"""Bound TCP/TLS/HTTP exchanges without abandoned request workers.

Platform DNS lookup cannot be interrupted; recheck the budget immediately after
it returns. Native installs keep this stdlib transport independent of Protoverta.
"""

from __future__ import annotations

import math
import socket
import ssl
import threading
import time
from http.client import HTTPConnection, HTTPResponse, HTTPSConnection
from typing import Any, Self
from urllib.request import HTTPHandler, HTTPSHandler, Request


def _abort(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


class Deadline:
    def __init__(self, seconds: float) -> None:
        if isinstance(seconds, bool) or not isinstance(seconds, int | float) or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("HTTP deadline must be a finite positive number")
        self.end = time.monotonic() + seconds
        self.sockets: list[socket.socket] = []
        self.lock = threading.Lock()
        self.cancelled = False
        self.timer = threading.Timer(seconds, self._expire)
        self.timer.name = "AdapterHTTPDeadline"
        self.timer.daemon = True

    def remaining(self) -> float:
        if self.cancelled:
            raise TimeoutError("Provider request was cancelled")
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Provider request exceeded its total deadline")
        return remaining

    def bind(self, sock: socket.socket) -> None:
        with self.lock:
            self.sockets.append(sock)
        try:
            self.remaining()
        except TimeoutError:
            _abort(sock)
            raise

    def _expire(self) -> None:
        with self.lock:
            sockets = self.sockets[:]
        for sock in sockets:
            _abort(sock)

    def cancel(self) -> None:
        self.cancelled = True
        self._expire()

    def __enter__(self) -> Self:
        self.timer.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.timer.cancel()
        self.timer.join()
        self.remaining()


def attach_deadline(request: Request, budget: Any) -> None:
    """Let the deadline transport find the budget (a ``Deadline``-like object) for ``request``."""
    request.adapter_deadline = budget  # type: ignore[attr-defined]


def _connection(kind: type[HTTPConnection], request: Request) -> type[HTTPConnection]:
    budget: Any = getattr(request, "adapter_deadline", None)
    if budget is None:
        return kind

    class Connection(kind):  # type: ignore[valid-type,misc]
        sock: Any

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._create_connection = self._connect_socket

        def _connect_socket(self, address: tuple[str, int], timeout: float, source_address: Any) -> socket.socket:
            # Platform DNS lookup cannot be interrupted by socket shutdown.
            # Recheck afterwards so cancellation never starts a late connect.
            addresses = socket.getaddrinfo(address[0], address[1], 0, socket.SOCK_STREAM)
            budget.remaining()
            error = None
            for family, sock_type, proto, _, target in addresses:
                sock = socket.socket(family, sock_type, proto)
                try:
                    sock.settimeout(min(timeout, budget.remaining()))
                    if source_address:
                        sock.bind(source_address)
                    budget.bind(sock)
                    sock.connect(target)
                    budget.remaining()
                    return sock
                except OSError as exc:
                    error = exc
                    sock.close()
                    budget.remaining()
            raise error or OSError("No usable provider address")

        def connect(self) -> None:
            HTTPConnection.connect(self)
            budget.remaining()
            if isinstance(self, HTTPSConnection):
                # The TLS handshake runs under the deadline, so it is done here
                # with the connection's own (http.client-built) context.
                tls = self._context.wrap_socket(self.sock, server_hostname=self._tunnel_host or self.host, do_handshake_on_connect=False)
                self.sock = tls
                budget.bind(tls)
                tls.settimeout(budget.remaining())
                tls.do_handshake()
                budget.remaining()

    return Connection


class DeadlineHTTPHandler(HTTPHandler):
    def http_open(self, request: Request) -> HTTPResponse:
        return self.do_open(_connection(HTTPConnection, request), request)


class DeadlineHTTPSHandler(HTTPSHandler):
    def __init__(self, debuglevel: int | None = None, context: ssl.SSLContext | None = None, check_hostname: bool | None = None) -> None:
        # Python 3.11 expects an integer debug level; later versions accept None.
        super().__init__(0 if debuglevel is None else debuglevel, context, check_hostname)
        # Kept here rather than read back from CPython's private attributes.
        # None lets http.client build its default verified context, as before.
        self._tls_context = context

    def https_open(self, request: Request) -> HTTPResponse:
        return self.do_open(_connection(HTTPSConnection, request), request, context=self._tls_context)
