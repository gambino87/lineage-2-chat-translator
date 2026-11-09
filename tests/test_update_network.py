"""Bounded socket tests use only a disposable loopback peer, never the release host."""
from contextlib import contextmanager
import socket
import ssl
import threading
import time
import unittest
from unittest.mock import patch
from urllib import request

import app_updater as updater
from update_network import (ControlledHTTPHandler, ControlledHTTPSHandler, NetworkOperation,
                            NetworkCancelled, NetworkDeadline, ControlledHTTPConnection)


@contextmanager
def peer(serve):
    stop = threading.Event()
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    listener.settimeout(2)
    port = listener.getsockname()[1]

    def run():
        try:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(2)
                serve(connection, stop)
        except (OSError, TimeoutError):
            pass

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        yield port
    finally:
        stop.set()
        listener.close()
        worker.join(3)


def header_drip(connection, stop):
    connection.recv(8192)
    for byte in b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}':
        if stop.wait(0.025):
            return
        connection.sendall(bytes([byte]))


class UpdateNetworkTests(unittest.TestCase):
    def test_header_drip_cannot_outlive_metadata_budget(self):
        with peer(header_drip) as port:
            url = f'http://127.0.0.1:{port}/release.json'
            def opener(operation):
                return request.build_opener(request.ProxyHandler({}), ControlledHTTPHandler(operation))
            began = time.monotonic()
            with patch.object(updater, 'RELEASE_URL', url), patch.object(updater, '_opener', opener), \
                 patch.object(updater, 'MAX_METADATA_SECONDS', 0.20), \
                 self.assertRaisesRegex(updater.UpdateError, 'took too long'):
                updater.check_for_update()
            self.assertLess(time.monotonic() - began, 0.9)

    def test_cancel_wakes_wait_for_headers(self):
        cancel = threading.Event()
        with peer(header_drip) as port:
            timer = threading.Timer(0.15, cancel.set)
            timer.start()
            began = time.monotonic()
            try:
                with self.assertRaises(updater.UpdateCancelled), updater._network_scope(3, cancel) as operation:
                    opener = request.build_opener(request.ProxyHandler({}), ControlledHTTPHandler(operation))
                    opener.open(f'http://127.0.0.1:{port}/', timeout=3)
            finally:
                timer.cancel()
            self.assertLess(time.monotonic() - began, 0.9)

    def test_cancel_wakes_body_read_even_without_another_progress_chunk(self):
        entered_body = threading.Event()
        def body_pause(connection, stop):
            connection.recv(8192)
            connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 1024\r\n\r\na')
            entered_body.set()
            stop.wait(2)
        cancel = threading.Event()
        with peer(body_pause) as port:
            def cancel_when_reading():
                if entered_body.wait(1):
                    time.sleep(0.05)
                    cancel.set()
            canceller = threading.Thread(target=cancel_when_reading, daemon=True)
            canceller.start()
            began = time.monotonic()
            with self.assertRaises(updater.UpdateCancelled), updater._network_scope(3, cancel) as operation:
                opener = request.build_opener(request.ProxyHandler({}), ControlledHTTPHandler(operation))
                with opener.open(f'http://127.0.0.1:{port}/', timeout=3) as response:
                    response.read(1024)
            canceller.join(1)
            self.assertLess(time.monotonic() - began, 0.9)

    def test_tls_handshake_is_covered_before_any_response_exists(self):
        def stalled_tls(connection, stop):
            connection.recv(8192)  # Read a ClientHello, then intentionally stay silent.
            stop.wait(2)
        with peer(stalled_tls) as port:
            began = time.monotonic()
            with self.assertRaises(NetworkDeadline), NetworkOperation(0.20) as operation:
                opener = request.build_opener(request.ProxyHandler({}), ControlledHTTPSHandler(
                    operation, context=ssl.create_default_context()))
                opener.open(f'https://127.0.0.1:{port}/', timeout=3)
            self.assertLess(time.monotonic() - began, 0.9)

    def test_socket_is_registered_before_connect_can_block(self):
        operation = NetworkOperation(2)
        class ConnectingSocket:
            def settimeout(self, _): pass
            def setsockopt(self, *_): pass
            def connect(self, _):
                self.registered = self in operation._sockets
            def close(self): pass
        sock = ConnectingSocket()
        with patch('update_network.socket.socket', return_value=sock):
            connection = ControlledHTTPConnection('127.0.0.1', 9, timeout=2, operation=operation)
            connection.connect()
        self.assertTrue(sock.registered)

    def test_dns_names_rejected_and_cancel_before_request_creates_no_socket(self):
        with NetworkOperation(2) as operation, patch('update_network.socket.socket') as create:
            with self.assertRaises(socket.gaierror):
                ControlledHTTPConnection('invalid.example', operation=operation, timeout=2).connect()
            create.assert_not_called()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(NetworkCancelled), patch('update_network.socket.socket') as create:
            with NetworkOperation(2, cancel):
                self.fail('Canceled operation entered')
        create.assert_not_called()


if __name__ == '__main__':
    unittest.main()
