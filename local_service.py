"""Authenticate the dedicated model process before sending chat or credentials.

Credentials exist only in this launcher's memory and its child environment.
Every HTTP connection is matched to its Windows TCP peer and a retained process
handle. A different process taking over the same port receives no request bytes.
This protects against other local accounts, not software already running as the
installing user or an administrator.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import http.client
import os
from pathlib import Path
import re
import socket
import struct
import threading
import time
from urllib import request


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x100000
TOKEN_QUERY = 0x0008
TOKEN_USER = 1
WAIT_TIMEOUT = 258
ERROR_INSUFFICIENT_BUFFER = 122
TCP_TABLE_OWNER_PID_ALL = 5
TCP_ESTABLISHED = 5
MAX_TCP_TABLE_BYTES = 16 * 1024 * 1024
TCP_OWNER_ATTEMPTS = 10
TCP_OWNER_RETRY_SECONDS = 0.01
_sessions = {}
_sessions_lock = threading.RLock()


class ServiceIdentityError(OSError):
    """The managed service is absent, replaced or no longer authenticated."""


def _windows():
    if os.name != "nt":
        raise ServiceIdentityError("The protected local service requires Windows.")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    return kernel


def _user_sid(handle):
    """Return the token user SID as bytes; no account-name lookup or shell."""
    kernel = _windows()
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    security.OpenProcessToken.restype = wintypes.BOOL
    security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    security.GetTokenInformation.restype = wintypes.BOOL
    security.GetLengthSid.argtypes = [ctypes.c_void_p]
    security.GetLengthSid.restype = wintypes.DWORD
    token = wintypes.HANDLE()
    if not security.OpenProcessToken(handle, TOKEN_QUERY, ctypes.byref(token)):
        raise ServiceIdentityError("Cannot verify the local service's Windows account.")
    try:
        size = wintypes.DWORD()
        security.GetTokenInformation(token, TOKEN_USER, None, 0, ctypes.byref(size))
        if not 0 < size.value <= 65536:
            raise ServiceIdentityError("Windows returned an invalid account record.")
        data = ctypes.create_string_buffer(size.value)
        if not security.GetTokenInformation(token, TOKEN_USER, data, size, ctypes.byref(size)):
            raise ServiceIdentityError("Cannot read the local service's Windows account.")
        sid = ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))[0]
        length = security.GetLengthSid(sid)
        if not 0 < length <= 68:
            raise ServiceIdentityError("Windows returned an invalid account identifier.")
        return ctypes.string_at(sid, length)
    finally:
        kernel.CloseHandle(token)


class ProcessIdentity:
    """Pin one exact process object; a reused PID cannot substitute for it."""
    def __init__(self, pid: int, executable: Path):
        self._lock = threading.RLock()
        self.handle = None
        self.pid = pid
        if type(pid) is not int or pid <= 0:
            raise ServiceIdentityError("The local service process identity is invalid.")
        kernel = _windows()
        self.handle = kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
        if not self.handle:
            raise ServiceIdentityError("Cannot retain the local service process identity.")
        try:
            image = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(image))
            if not kernel.QueryFullProcessImageNameW(self.handle, 0, image, ctypes.byref(size)):
                raise ServiceIdentityError("Cannot verify the local service executable.")
            if os.path.normcase(os.path.abspath(image.value)) != os.path.normcase(os.path.abspath(executable)):
                raise ServiceIdentityError("The local service executable does not match this installation.")
            current = kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, os.getpid())
            if not current:
                raise ServiceIdentityError("Cannot verify the current Windows account.")
            try:
                if _user_sid(self.handle) != _user_sid(current):
                    raise ServiceIdentityError("The local service belongs to a different Windows account.")
            finally:
                kernel.CloseHandle(current)
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(self.handle, *(ctypes.byref(value) for value in times)):
                raise ServiceIdentityError("Cannot verify the local service creation time.")
            self.created = times[0].dwLowDateTime | (times[0].dwHighDateTime << 32)
            self.check_alive()
        except Exception:
            self.close()
            raise

    def check_alive(self):
        with self._lock:
            if not self.handle or _windows().WaitForSingleObject(self.handle, 0) != WAIT_TIMEOUT:
                raise ServiceIdentityError("The protected translation service stopped. Reopen the translator.")

    def close(self):
        with self._lock:
            if self.handle:
                _windows().CloseHandle(self.handle)
                self.handle = None


def _tcp_rows():
    """Read kernel IPv4 ownership records, with bounded retries for table growth."""
    _windows()
    api = ctypes.WinDLL("iphlpapi", use_last_error=True).GetExtendedTcpTable
    api.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL, wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
    api.restype = wintypes.DWORD
    size = wintypes.DWORD()
    result = api(None, ctypes.byref(size), False, socket.AF_INET, TCP_TABLE_OWNER_PID_ALL, 0)
    for _ in range(4):
        if result != ERROR_INSUFFICIENT_BUFFER or not 4 <= size.value <= MAX_TCP_TABLE_BYTES:
            break
        data = ctypes.create_string_buffer(size.value)
        result = api(data, ctypes.byref(size), False, socket.AF_INET, TCP_TABLE_OWNER_PID_ALL, 0)
        if result == 0:
            count = struct.unpack_from("<I", data)[0]
            if 4 + count * 24 > size.value:
                break
            return [struct.unpack_from("<6I", data, 4 + i * 24) for i in range(count)]
    raise ServiceIdentityError("Windows could not identify the connected local service.")


def _address(value):
    return socket.inet_ntoa(struct.pack("<I", value))


def verify_socket(sock, identity):
    """Match the reverse four-tuple of THIS connected socket before HTTP send."""
    identity.check_alive()
    client = sock.getsockname()
    server = sock.getpeername()
    if client[0] != "127.0.0.1" or server[0] != "127.0.0.1":
        raise ServiceIdentityError("The protected service connection is not local.")
    # The server's final handshake/table entry can trail connect() briefly.
    # Retry only a missing tuple; a positively identified wrong owner is final.
    for attempt in range(TCP_OWNER_ATTEMPTS):
        owners = {pid for state, local, local_port, remote, remote_port, pid in _tcp_rows()
                  if state == TCP_ESTABLISHED and (_address(local), socket.ntohs(local_port & 0xffff)) == server
                  and (_address(remote), socket.ntohs(remote_port & 0xffff)) == client}
        if owners or attempt == TCP_OWNER_ATTEMPTS - 1:
            break
        identity.check_alive()
        time.sleep(TCP_OWNER_RETRY_SECONDS)
    if owners != {identity.pid}:
        raise ServiceIdentityError("The local service connection belongs to a different process. No chat was sent.")
    # A process can exit while the table is being read. The retained object
    # cannot turn into a new process even if its numeric PID is later reused.
    identity.check_alive()


class ServiceSession:
    def __init__(self, endpoint, identity, credential):
        if not re.fullmatch(r"[0-9a-f]{64}", credential):
            raise ServiceIdentityError("Invalid local service credential.")
        self.endpoint = endpoint.rstrip("/")
        self.identity = identity
        self._credential = credential

    def authorize(self, sock):
        verify_socket(sock, self.identity)

    def header(self):
        self.identity.check_alive()
        return "Bearer " + self._credential


def register_session(endpoint, process, executable, credential):
    identity = ProcessIdentity(process.pid, executable)
    try:
        session = ServiceSession(endpoint, identity, credential)
    except Exception:
        identity.close()
        raise
    with _sessions_lock:
        old = _sessions.pop(session.endpoint, None)
        if old:
            old.identity.close()
        _sessions[session.endpoint] = session
    return session


def get_session(endpoint):
    with _sessions_lock:
        session = _sessions.get(endpoint.rstrip("/"))
        if session is None:
            raise ServiceIdentityError("No authenticated translation service is available. Open the translator launcher.")
        session.identity.check_alive()
        return session


def forget_session(endpoint, session):
    if session is None:
        return
    with _sessions_lock:
        if _sessions.get(endpoint.rstrip("/")) is session:
            _sessions.pop(endpoint.rstrip("/"), None)
        if session:
            session.identity.close()


class PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, session, **kwargs):
        self.session = session
        super().__init__(*args, **kwargs)

    def connect(self):
        try:
            super().connect()
            self.session.authorize(self.sock)
        except Exception:
            self.close()
            raise


class PinnedHTTPHandler(request.HTTPHandler):
    def __init__(self, endpoint):
        super().__init__()
        self.endpoint = endpoint.rstrip("/")

    def http_open(self, req):
        if req.full_url.split("/", 3)[:3] != self.endpoint.split("/", 3)[:3]:
            raise ServiceIdentityError("The service request left its protected endpoint.")
        session = get_session(self.endpoint)
        req.add_unredirected_header("Authorization", session.header())
        return self.do_open(lambda *args, **kwargs: PinnedHTTPConnection(*args, session=session, **kwargs), req)
