"""Bound TCP/TLS/HTTP exchanges without abandoned request workers.

Platform DNS lookup cannot be interrupted; recheck the budget immediately after
it returns. Native installs keep this stdlib transport independent of Protoverta.
"""

import math
import socket
import threading
import time
from http.client import HTTPConnection, HTTPSConnection
from urllib.request import HTTPHandler, HTTPSHandler


def _abort(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


class Deadline:
    def __init__(self, seconds):
        if isinstance(seconds, bool) or not isinstance(seconds, int | float) or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("HTTP deadline must be a finite positive number")
        self.end = time.monotonic() + seconds
        self.sockets = []
        self.lock = threading.Lock()
        self.cancelled = False
        self.timer = threading.Timer(seconds, self._expire)
        self.timer.name = "AdapterHTTPDeadline"
        self.timer.daemon = True

    def remaining(self):
        if self.cancelled:
            raise TimeoutError("Provider request was cancelled")
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Provider request exceeded its total deadline")
        return remaining

    def bind(self, sock):
        with self.lock:
            self.sockets.append(sock)
        try:
            self.remaining()
        except TimeoutError:
            _abort(sock)
            raise

    def _expire(self):
        with self.lock:
            sockets = self.sockets[:]
        for sock in sockets:
            _abort(sock)

    def cancel(self):
        self.cancelled = True
        self._expire()

    def __enter__(self):
        self.timer.start()
        return self

    def __exit__(self, *_):
        self.timer.cancel()
        self.timer.join()
        self.remaining()


def _connection(kind, request):
    budget = getattr(request, "adapter_deadline", None)

    class Connection(kind):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if budget is not None:
                self._create_connection = self._connect_socket

        def _connect_socket(self, address, timeout, source_address):
            # Platform DNS lookup cannot be interrupted by socket shutdown.
            # Recheck afterwards so cancellation never starts a late connect.
            addresses = socket.getaddrinfo(*address, 0, socket.SOCK_STREAM)
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

        def connect(self):
            if budget is None:
                return super().connect()
            HTTPConnection.connect(self)
            budget.remaining()
            if isinstance(self, HTTPSConnection):
                self.sock = self._context.wrap_socket(self.sock, server_hostname=self._tunnel_host or self.host, do_handshake_on_connect=False)
                budget.bind(self.sock)
                self.sock.settimeout(budget.remaining())
                self.sock.do_handshake()
                budget.remaining()

    return Connection


class DeadlineHTTPHandler(HTTPHandler):
    def http_open(self, request):
        return self.do_open(_connection(HTTPConnection, request), request)


class DeadlineHTTPSHandler(HTTPSHandler):
    def https_open(self, request):
        return self.do_open(_connection(HTTPSConnection, request), request, context=self._context)
