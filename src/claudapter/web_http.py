"""Bounded setup HTTP transport using only the Python standard library."""

import io
import math
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _InputDeadline(io.RawIOBase):
    def __init__(self, raw, connection, deadline):
        self.raw, self.connection, self.deadline = raw, connection, deadline

    def readable(self):
        return True

    def remaining(self):
        seconds = self.deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("Setup input deadline exceeded")
        return seconds

    def readinto(self, buffer):
        self.connection.settimeout(self.remaining())
        count = self.raw.readinto(buffer)
        self.remaining()
        return count

    def close(self):
        try:
            self.raw.close()
        finally:
            super().close()


class SetupRequestHandler(BaseHTTPRequestHandler):
    header_timeout = 10.0
    body_timeout = 15.0
    input_timeout = 30.0

    def setup(self):
        super().setup()
        for value in (self.header_timeout, self.body_timeout, self.input_timeout):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("Setup input timeouts must be finite positive numbers")
        now = time.monotonic()
        self._input_end = now + self.input_timeout
        self.rfile.close()
        self._deadline_input = _InputDeadline(self.connection.makefile("rb", buffering=0), self.connection, min(self._input_end, now + self.header_timeout))
        self.rfile = io.BufferedReader(self._deadline_input)

    def parse_request(self):
        valid = super().parse_request()
        if valid:
            self._deadline_input.remaining()
            self._deadline_input.deadline = min(self._input_end, time.monotonic() + self.body_timeout)
        return valid

    def handle(self):
        try:
            super().handle()
        except (ConnectionError, TimeoutError):
            # Incomplete/closed clients need no traceback or response fabrication.
            return

    def end_headers(self):
        self.connection.settimeout(2.0)
        super().end_headers()


class SetupHTTPServer(ThreadingHTTPServer):
    """Bound even unauthenticated connections; close pending clients on exit."""

    daemon_threads = True

    def __init__(self, address, handler, *, max_connections=16):
        if type(max_connections) is not int or max_connections < 1:
            raise ValueError("Setup connection limit must be a positive integer")
        self._slots = threading.BoundedSemaphore(max_connections)
        self._active = set()
        self._active_lock = threading.Lock()
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        with self._active_lock:
            self._active.add(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self._active_lock:
                self._active.discard(request)
            self._slots.release()
            self.shutdown_request(request)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._active_lock:
                self._active.discard(request)
            self._slots.release()

    def server_close(self):
        super().server_close()
        with self._active_lock:
            clients = tuple(self._active)
        for connection in clients:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
