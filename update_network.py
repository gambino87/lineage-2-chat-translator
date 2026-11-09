"""Interruptible update HTTP transport with one wall-clock budget.

The updater uses a fixed numeric HTTPS origin, bypasses proxies and never needs
DNS. Register sockets before connect/TLS/header reads so cancellation can wake
every blocking network stage, including a server that drips headers forever.
"""
from __future__ import annotations

import http.client
import socket
import threading
import time
from urllib import request

CANCELLATION_POLL_SECONDS = 0.025


class NetworkCancelled(RuntimeError):
    pass


class NetworkDeadline(RuntimeError):
    pass


class NetworkOperation:
    def __init__(self, seconds, cancel_event=None):
        self.deadline = time.monotonic() + seconds
        self.cancel_event = cancel_event
        self._finished = threading.Event()
        self._lock = threading.Lock()
        self._sockets = []
        self._watcher = None

    def check(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise NetworkCancelled('The update request was canceled.')
        if time.monotonic() >= self.deadline:
            raise NetworkDeadline('The update request exceeded its time budget.')

    def remaining(self):
        self.check()
        return max(0.001, self.deadline - time.monotonic())

    def add_socket(self, sock):
        with self._lock:
            self._sockets.append(sock)
        try:
            self.check()
        except (NetworkCancelled, NetworkDeadline):
            self._abort()
            raise

    def _abort(self):
        with self._lock:
            for sock in self._sockets:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                # Closing the buffered response from here can deadlock against
                # its reader. Wake the raw socket instead (also on WinSock).
                try:
                    sock._real_close()
                except OSError:
                    pass

    def _watch(self):
        while not self._finished.wait(CANCELLATION_POLL_SECONDS):
            try:
                self.check()
            except (NetworkCancelled, NetworkDeadline):
                self._abort()
                return

    def __enter__(self):
        self.check()
        self._watcher = threading.Thread(target=self._watch, name='update-network-budget', daemon=True)
        self._watcher.start()
        return self

    def __exit__(self, *_):
        self._finished.set()
        if self._watcher is not None:
            self._watcher.join()
        self.check()


def _connect(connection, operation):
    operation.check()
    # AI_NUMERICHOST makes the fixed-host rule explicit and avoids an OS DNS
    # resolver call that cannot be interrupted with socket shutdown.
    addresses = socket.getaddrinfo(connection.host, connection.port, type=socket.SOCK_STREAM,
                                   flags=socket.AI_NUMERICHOST)
    last_error = None
    for family, kind, proto, _, address in addresses:
        operation.check()
        sock = socket.socket(family, kind, proto)
        operation.add_socket(sock)
        try:
            sock.settimeout(min(connection.timeout, operation.remaining()))
            if connection.source_address:
                sock.bind(connection.source_address)
            sock.connect(address)
            connection.sock = sock
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if connection._tunnel_host:
                connection._tunnel()
            return
        except OSError as exc:
            sock.close()
            operation.check()
            last_error = exc
    if last_error is not None:
        raise last_error
    raise OSError('The update host has no numeric address.')


class ControlledHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, operation, **kwargs):
        self.operation = operation
        super().__init__(*args, **kwargs)

    def connect(self):
        _connect(self, self.operation)


class ControlledHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, operation, **kwargs):
        self.operation = operation
        super().__init__(*args, **kwargs)

    def connect(self):
        _connect(self, self.operation)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self._tunnel_host or self.host,
                                              do_handshake_on_connect=False)
        self.operation.add_socket(self.sock)
        self.sock.do_handshake()


class ControlledHTTPHandler(request.HTTPHandler):
    def __init__(self, operation):
        super().__init__()
        self.operation = operation

    def http_open(self, req):
        return self.do_open(lambda *args, **kwargs: ControlledHTTPConnection(
            *args, operation=self.operation, **kwargs), req)


class ControlledHTTPSHandler(request.HTTPSHandler):
    def __init__(self, operation, *, context):
        super().__init__(context=context)
        self.operation = operation

    def https_open(self, req):
        return self.do_open(lambda *args, **kwargs: ControlledHTTPSConnection(
            *args, operation=self.operation, **kwargs), req, context=self._context)
