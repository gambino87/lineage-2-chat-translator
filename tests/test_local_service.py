"""Connection identity tests use inert in-process sockets, never Ollama/apps."""
import ctypes
import http.client
import os
from pathlib import Path
import socket
import struct
import sys
import threading
import unittest
from unittest.mock import Mock, patch

import local_service as service


def row(local, remote, pid):
    return (5, struct.unpack("<I", socket.inet_aton(local[0]))[0], socket.htons(local[1]),
            struct.unpack("<I", socket.inet_aton(remote[0]))[0], socket.htons(remote[1]), pid)


class SocketIdentityTests(unittest.TestCase):
    def setUp(self):
        self.sock = Mock()
        self.sock.getsockname.return_value = ("127.0.0.1", 44111)
        self.sock.getpeername.return_value = ("127.0.0.1", 11435)
        self.identity = Mock(pid=1234)

    def test_checks_actual_reverse_tuple_and_live_process_twice(self):
        rows = [row(self.sock.getpeername(), self.sock.getsockname(), 1234),
                row(("127.0.0.1", 11435), ("127.0.0.1", 44112), 9999)]
        with patch.object(service, "_tcp_rows", return_value=rows):
            service.verify_socket(self.sock, self.identity)
        self.assertEqual(self.identity.check_alive.call_count, 2)

    def test_other_process_same_port_is_rejected(self):
        with patch.object(service, "_tcp_rows", return_value=[row(self.sock.getpeername(), self.sock.getsockname(), 9999)]):
            with self.assertRaisesRegex(service.ServiceIdentityError, "different process"):
                service.verify_socket(self.sock, self.identity)

    def test_same_pid_different_socket_is_rejected(self):
        with patch.object(service, "_tcp_rows", return_value=[row(("127.0.0.1", 11435), ("127.0.0.1", 44112), 1234)]):
            with self.assertRaises(service.ServiceIdentityError):
                service.verify_socket(self.sock, self.identity)

    def test_process_exit_during_check_is_rejected(self):
        self.identity.check_alive.side_effect = [None, service.ServiceIdentityError("stopped")]
        with patch.object(service, "_tcp_rows", return_value=[row(self.sock.getpeername(), self.sock.getsockname(), 1234)]):
            with self.assertRaisesRegex(service.ServiceIdentityError, "stopped"):
                service.verify_socket(self.sock, self.identity)

    def test_transient_missing_kernel_tuple_can_be_retried_without_sending(self):
        with patch.object(service, "_tcp_rows", side_effect=[[], [row(self.sock.getpeername(), self.sock.getsockname(), 1234)]]), \
             patch.object(service.time, "sleep"):
            service.verify_socket(self.sock, self.identity)

    def test_nonlocal_peer_is_rejected_before_table_lookup(self):
        self.sock.getpeername.return_value = ("192.0.2.1", 11435)
        with patch.object(service, "_tcp_rows") as table:
            with self.assertRaises(service.ServiceIdentityError):
                service.verify_socket(self.sock, self.identity)
        table.assert_not_called()

    def test_missing_session_fails_without_connecting(self):
        with self.assertRaisesRegex(service.ServiceIdentityError, "No authenticated"):
            service.get_session("http://127.0.0.1:1")

    def test_credentials_are_validated_and_not_in_repr(self):
        secret = "ab" * 32
        session = service.ServiceSession("http://127.0.0.1:11435", self.identity, secret)
        self.assertNotIn(secret, repr(session))
        for invalid in ("", "x" * 64, secret + "\r\n"):
            with self.assertRaises(service.ServiceIdentityError):
                service.ServiceSession("http://127.0.0.1:11435", self.identity, invalid)

    def test_failed_identity_sends_no_http_headers_or_body(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.addCleanup(listener.close)
        captured = []
        def accept():
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(2)
                captured.append(conn.recv(4096))
        worker = threading.Thread(target=accept)
        worker.start()
        session = Mock()
        session.authorize.side_effect = service.ServiceIdentityError("replacement")
        connection = service.PinnedHTTPConnection("127.0.0.1", listener.getsockname()[1], session=session, timeout=2)
        with self.assertRaises(service.ServiceIdentityError):
            connection.request("POST", "/api/chat", b"private synthetic message", {"Authorization": "Bearer synthetic"})
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(captured, [b""])

    @unittest.skipUnless(os.name == "nt", "Windows kernel identity integration")
    def test_real_current_process_socket_matches_kernel_owner(self):
        # Windows venv python.exe is a redirector; the actual image is the base interpreter.
        identity = service.ProcessIdentity(os.getpid(), Path(getattr(sys, "_base_executable", sys.executable)))
        self.addCleanup(identity.close)
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        client = socket.create_connection(listener.getsockname(), timeout=2)
        self.addCleanup(client.close)
        server, _ = listener.accept()
        self.addCleanup(server.close)
        service.verify_socket(client, identity)
        self.assertGreater(identity.created, 0)

    @unittest.skipUnless(os.name == "nt", "Windows process identity integration")
    def test_real_process_image_mismatch_is_rejected(self):
        with self.assertRaisesRegex(service.ServiceIdentityError, "does not match"):
            service.ProcessIdentity(os.getpid(), Path(sys.executable).with_name("not-the-process.exe"))
