"""Portable, per-user Ollama setup for the packaged L2 Chat Translator.

Only the selected public model is downloaded. All model API traffic is sent to
the dedicated loopback service after Windows verifies its executable owner.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
import errno
import hashlib
import http.client
from http.client import HTTPException
import json
import os
from pathlib import Path, PureWindowsPath
import re
import secrets
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
from typing import Callable
from urllib import error, request
import zipfile

from ui_i18n import tr

from local_service import ServiceIdentityError, get_session, register_session, forget_session
from secure_storage import StorageError, guard_private_tree, safe_remove_tree
from runtime_integrity import (BACKEND_VERSION, pin_runtime, verified_runtime,
                               verify_bundled_frontend, load_manifest)


OLLAMA_VERSION = "0.40.14"
ARCHIVE_SHA256 = "65d68cade6cf6375f398b518224f8c8a15e062387f38cf477abfc848de7e7e29"
ARCHIVE_URL = f"https://github.com/ollama/ollama/releases/download/v{OLLAMA_VERSION}/ollama-windows-amd64.zip"
PORT = 11435
ENDPOINT = f"http://127.0.0.1:{PORT}"
DEFAULT_MODEL = "qwen3.5:9b-q4_K_M"
MIB = 1024 ** 2
GIB = 1024 ** 3
# Includes the download archive, extracted GPU libraries and working space.
RUNTIME_PEAK_BYTES = 5 * GIB
RUNTIME_INSTALLED_ALLOWANCE_BYTES = int(2.5 * GIB)
MODEL_FREE_SPACE_MARGIN_BYTES = GIB
# The pinned archive is about 1.6 GB. Review this ceiling when changing its pin;
# a checksum alone rejects bad downloads only after they have filled the disk.
MAX_RUNTIME_ARCHIVE_BYTES = 2 * GIB
MAX_EXTRACTED_RUNTIME_BYTES = 8 * GIB
TRANSFER_CHUNK_BYTES = MIB
MAX_API_RESPONSE_BYTES = 4 * MIB
MAX_PULL_STATUS_BYTES = MIB
DOWNLOAD_PROGRESS_BYTES = 50 * MIB
DOWNLOAD_PROGRESS_PERCENT_STEP = 2
DECIMAL_MB = 1_000_000

# Keep UI waits bounded separately from long model downloads.
RUNTIME_DOWNLOAD_IDLE_SECONDS = 60
RUNTIME_DOWNLOAD_BUDGET_SECONDS = 60 * 60
API_TIMEOUT_SECONDS = 30
OWNER_CHECK_SECONDS = 20
OWNER_PROBE_SECONDS = 3
SHUTDOWN_OWNER_CHECK_SECONDS = 5
MODEL_PULL_IDLE_SECONDS = 600
MODEL_PULL_BUDGET_SECONDS = 2 * 60 * 60
SETUP_CLIENT_BUDGET_SECONDS = 180
NETWORK_CONNECT_SECONDS = 5
CANCELLATION_POLL_SECONDS = 0.1
MODEL_UNLOAD_BUDGET_SECONDS = 6
MAX_MODELS_TO_UNLOAD = 4
SERVICE_STARTUP_SECONDS = 60
SERVICE_STARTUP_POLL_SECONDS = 0.5
SERVICE_STOP_SECONDS = 10
SERVICE_STOP_POLL_SECONDS = 0.25
MODEL_CONTEXT_TOKENS = 2048
MODEL_KEEP_ALIVE = "5m"

# Win32 flags and buffer limits are platform contracts, not tunable policy.
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
SYNCHRONIZE = 0x100000
WAIT_OBJECT_0 = 0
WINDOWS_PATH_BUFFER_CHARS = 32768
MODEL_CATALOG = {
    DEFAULT_MODEL: {
        "label": "Qwen3.5 9B — recommended",
        "download_gb": 6.6, "download_bytes": 6_600_000_000,
        "tested": True, "recommended": True,
        "license": "Apache 2.0", "license_url": "https://www.apache.org/licenses/LICENSE-2.0",
        "description": "Current tested model. Prefer 16 GB RAM and a GPU with ample free memory; CPU use is slower. Greeklish can still be inaccurate.",
    },
    "qwen2.5:3b": {
        "label": "Qwen2.5 3B — lightweight",
        "download_gb": 1.9, "download_bytes": 1_930_000_000,
        "tested": True, "recommended": False,
        "license": "Qwen Research License", "license_url": "https://huggingface.co/Qwen/Qwen2.5-3B-Instruct/blob/main/LICENSE",
        "description": "Previously tested lightweight fallback. Lower quality for slang, Greeklish and difficult sentences. Review the model's research license before use.",
    },
    "qwen3.5:4b-q4_K_M": {
        "label": "Qwen3.5 4B — experimental",
        "download_gb": 3.3, "download_bytes": 3_300_000_000,
        "tested": False, "recommended": False,
        "license": "Apache 2.0", "license_url": "https://www.apache.org/licenses/LICENSE-2.0",
        "description": "Smaller alternative. Not validated against this application's translation benchmark; accuracy and performance are unverified.",
    },
}


def required_disk_bytes(tag: str, *, include_runtime: bool = True) -> int:
    """Conservative fresh-setup free-space estimate; model downloads vary."""
    if tag not in MODEL_CATALOG:
        raise SetupError(tr('Choose a supported model from the model list.'))
    model_with_margin = MODEL_CATALOG[tag]["download_bytes"] + MODEL_FREE_SPACE_MARGIN_BYTES
    return max(RUNTIME_PEAK_BYTES, model_with_margin + RUNTIME_INSTALLED_ALLOWANCE_BYTES) if include_runtime else model_with_margin


class SetupError(RuntimeError):
    """A recoverable setup failure suitable for display in the setup window."""


class ServiceOwnershipMismatch(SetupError):
    """A verified loopback listener uses a different runtime executable."""


class SetupCancelled(SetupError):
    """The user cancelled setup; completed model layers remain available."""


class _NetworkOperation:
    """Interrupt socket I/O without racing a buffered response's close lock.

    A separate wall-clock budget also covers a peer that keeps sending headers
    or a partial JSON line. Only sockets created for this operation are touched.
    DNS resolution remains subject to the operating system's resolver timeout.
    """
    def __init__(self, cancel_event, seconds, description):
        self.cancel_event = cancel_event
        self.deadline = time.monotonic() + seconds
        self.description = description
        self._finished = threading.Event()
        self._lock = threading.Lock()
        self._sockets = []
        self._watcher = None

    def check(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise SetupCancelled(tr("Setup cancelled. Downloaded model layers are kept for a later retry."))
        if time.monotonic() >= self.deadline:
            raise SetupError(tr(self.description) + tr(' exceeded its time budget. Retry setup to resume downloaded model layers.'))

    def remaining(self):
        self.check()
        return self.deadline - time.monotonic()

    def add_socket(self, sock):
        with self._lock:
            self._sockets.append(sock)
        try:
            self.check()
        except SetupError:
            self._abort()
            raise

    def _abort(self):
        with self._lock:
            for sock in self._sockets:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                # WinSock select() does not reliably wake on shutdown alone.
                # Close the socket itself, bypassing makefile's reference count;
                # never close the buffered HTTP response from this thread.
                try:
                    sock._real_close()
                except OSError:
                    pass

    def _watch(self):
        while not self._finished.wait(CANCELLATION_POLL_SECONDS):
            try:
                self.check()
            except SetupError:
                self._abort()
                return

    def __enter__(self):
        self.check()
        self._watcher = threading.Thread(target=self._watch, name="setup-network-cancellation", daemon=True)
        self._watcher.start()
        return self

    def __exit__(self, *_):
        self._finished.set()
        if self._watcher is not None:
            self._watcher.join()
        self.check()


def _controlled_connect(connection, operation):
    # Match HTTPConnection's TCP/tunnel setup, registering before CONNECT
    # headers or TLS can block. TCP connect has its own short timeout; a native
    # DNS lookup cannot be interrupted by Python socket shutdown.
    operation.check()
    connection.sock = connection._create_connection(
        (connection.host, connection.port), min(NETWORK_CONNECT_SECONDS, operation.remaining()),
        connection.source_address)
    operation.add_socket(connection.sock)
    connection.sock.settimeout(min(connection.timeout, operation.remaining()))
    try:
        connection.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError as exc:
        if exc.errno != errno.ENOPROTOOPT:
            raise
    if connection._tunnel_host:
        connection._tunnel()


class _ControlledHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, operation, session=None, **kwargs):
        self.operation = operation
        self.session = session
        super().__init__(*args, **kwargs)

    def connect(self):
        try:
            _controlled_connect(self, self.operation)
            if self.session is not None:
                self.session.authorize(self.sock)
        except Exception:
            self.close()
            raise


class _ControlledHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, operation, **kwargs):
        self.operation = operation
        super().__init__(*args, **kwargs)

    def connect(self):
        _controlled_connect(self, self.operation)
        # Register the SSL socket before its handshake; wrap_socket's default
        # handshake would leave the watchdog holding a detached TCP socket.
        self.sock = self._context.wrap_socket(
            self.sock, server_hostname=self._tunnel_host or self.host,
            do_handshake_on_connect=False)
        self.operation.add_socket(self.sock)
        self.sock.do_handshake()


class _ControlledHTTPHandler(request.HTTPHandler):
    def __init__(self, operation, session=None):
        super().__init__()
        self.operation = operation
        self.session = session

    def http_open(self, req):
        session = self.session() if self.session else None
        if session is not None:
            req.add_unredirected_header("Authorization", session.header())
        return self.do_open(lambda *args, **kwargs: _ControlledHTTPConnection(
            *args, operation=self.operation(), session=session, **kwargs), req)


class _ControlledHTTPSHandler(request.HTTPSHandler):
    def __init__(self, operation):
        super().__init__()
        self.operation = operation

    def https_open(self, req):
        return self.do_open(lambda *args, **kwargs: _ControlledHTTPSConnection(
            *args, operation=self.operation(), **kwargs), req, context=self._context)


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SetupError(tr('The local service tried to redirect a request. Setup stopped.'))


class _HttpsRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            raise SetupError(tr('The runtime download tried to leave HTTPS. Setup stopped.'))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _sha256(path: Path, check: Callable[[], None] | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(TRANSFER_CHUNK_BYTES), b""):
            if check is not None:
                check()
            digest.update(block)
    return digest.hexdigest()


def _service_environment(models_dir: Path, service_home: Path, credential: str) -> dict:
    """Keep user GPU visibility/proxies, but never inherit alternate code paths."""
    import ctypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    directories = []
    for name in ("GetSystemDirectoryW", "GetWindowsDirectoryW"):
        function = getattr(kernel, name)
        function.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
        function.restype = ctypes.c_uint
        buffer = ctypes.create_unicode_buffer(WINDOWS_PATH_BUFFER_CHARS)
        length = function(buffer, len(buffer))
        if not 0 < length < len(buffer):
            raise SetupError(tr('Windows could not provide the protected service search paths.'))
        directories.append(buffer.value)
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(("OLLAMA_", "GGML_", "LLAMA_", "L2CT_"))
                   and key.upper() not in {"PATH", "USERPROFILE", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"}}
    environment.update({"PATH": os.pathsep.join(directories), "USERPROFILE": str(service_home),
                        "OLLAMA_HOST": f"127.0.0.1:{PORT}", "OLLAMA_NO_CLOUD": "1",
                        "OLLAMA_MODELS": str(models_dir), "OLLAMA_CONTEXT_LENGTH": str(MODEL_CONTEXT_TOKENS),
                        "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_MAX_LOADED_MODELS": "1", "OLLAMA_KEEP_ALIVE": MODEL_KEEP_ALIVE,
                        "OLLAMA_DEBUG": "0", "OLLAMA_DEBUG_LOG_REQUESTS": "0", "OLLAMA_ORIGINS": "",
                        "L2CT_OLLAMA_API_KEY": credential})
    return environment


def _model_tag(tag: str) -> str:
    if not isinstance(tag, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*:[A-Za-z0-9][A-Za-z0-9_.-]*", tag) or "cloud" in tag.lower():
        raise SetupError(tr('Choose a downloaded local model from the model list.'))
    return tag


def _is_reparse_path(path: Path) -> bool:
    """Treat Windows junctions like symlinks when removing setup leftovers."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def _extract_archive(archive: Path, destination: Path, check: Callable[[], None] | None = None) -> None:
    """Validate every archive member before writing anything into staging."""
    root = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        seen = set()
        for member in members:
            if check is not None:
                check()
            windows_path = PureWindowsPath(member.filename)
            normalized = member.filename.replace("\\", "/")
            parts = normalized.split("/")
            target = (root / normalized).resolve()
            mode = member.external_attr >> 16
            if (windows_path.is_absolute() or windows_path.drive or normalized.startswith("/")
                    or ".." in parts or ":" in normalized or not target.is_relative_to(root)
                    or stat.S_ISLNK(mode)
                    or any(PureWindowsPath(part).is_reserved() or part.endswith((".", " "))
                           for part in parts if part not in ("", "."))):
                raise SetupError(tr('The runtime archive contains an unsafe path; no runtime was installed.'))
            key = str(target).casefold()
            if key in seen:
                raise SetupError(tr('The runtime archive contains duplicate paths.'))
            seen.add(key)
        if sum(item.file_size for item in members) > MAX_EXTRACTED_RUNTIME_BYTES:
            raise SetupError(tr('The runtime archive is larger than the supported installation size.'))
        for member in members:
            if check is not None:
                check()
            target = root / member.filename.replace("\\", "/")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, target.open("wb") as output:
                    for block in iter(lambda: source.read(TRANSFER_CHUNK_BYTES), b""):
                        if check is not None:
                            check()
                        output.write(block)


class RuntimeManager:
    def __init__(self, data_root: str | Path, progress: Callable[[str], None] | None = None,
                 *, cancel_event: threading.Event | None = None):
        if not str(data_root).strip():
            raise SetupError(tr('Choose a folder for the runtime and model files.'))
        # Keep the lexical path until the no-follow storage guard checks every
        # component; resolve() here would hide a junction chosen by another user.
        self.data_root = Path(os.path.abspath(Path(data_root).expanduser()))
        self.runtime_dir = self.data_root / "ollama"
        self.models_dir = self.data_root / "models"
        self.logs_dir = self.data_root / "logs"
        self.service_home = self.data_root / "service-home"
        self.executable = self.runtime_dir / "ollama.exe"
        self.receipt_path = self.runtime_dir / "verified-install.json"
        self.progress = progress or (lambda message: None)
        self.cancel_event = cancel_event
        self._ignore_cancel = False
        self._network = None
        self._opener = request.build_opener(request.ProxyHandler({}), _NoRedirect(),
                                           _ControlledHTTPHandler(lambda: self._network, lambda: get_session(ENDPOINT)))
        # Retain the child handle, not just its PID: configuration must never
        # assume that reusing an existing listener makes it ours to stop.
        self._owned_service: subprocess.Popen | None = None
        self._session = None
        self._storage_guard = None
        self._runtime_locks = None

    def _protect_storage(self):
        """Keep the owner's private tree stable through the service lifetime."""
        if self._storage_guard is not None:
            return
        from uninstall_data import authorize_data_root
        guard = guard_private_tree(self.data_root, create=True,
                                   authorize_existing=authorize_data_root, allow_locked_ancestors=True)
        try:
            guard.__enter__()
        except OSError as exc:
            raise SetupError(str(exc)) from exc
        self._storage_guard = guard

    def _release_storage(self):
        if self._runtime_locks is not None:
            self._runtime_locks.close()
            self._runtime_locks = None
        if self._storage_guard is not None:
            self._storage_guard.__exit__(None, None, None)
            self._storage_guard = None

    def check_cancelled(self):
        if not self._ignore_cancel and self.cancel_event is not None and self.cancel_event.is_set():
            raise SetupCancelled(tr("Setup cancelled. Downloaded model layers are kept for a later retry."))

    @contextmanager
    def _network_scope(self, seconds, description):
        previous = self._network
        token = None if self._ignore_cancel else self.cancel_event
        if previous is not None:
            seconds = min(seconds, previous.remaining())
        try:
            with _NetworkOperation(token, seconds, description) as operation:
                self._network = operation
                yield operation
        finally:
            self._network = previous

    @contextmanager
    def cancellable_client(self, client, timeout=SETUP_CLIENT_BUDGET_SECONDS):
        """Use bounded owned-service requests for setup's existing LLM pipeline."""
        original = client._request
        deadline = time.monotonic() + timeout
        def local_request(path, payload=None, timeout=SETUP_CLIENT_BUDGET_SECONDS):
            self.check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SetupError(tr('Setup verification exceeded its time budget. Retry setup.'))
            return self._request_api(path, payload, timeout=min(timeout, remaining))
        client._request = local_request
        try:
            self.check_cancelled()
            yield client
            self.check_cancelled()
        finally:
            client._request = original

    def _verified_runtime(self) -> bool:
        """The signed application's inventory is the authority, not local JSON."""
        self.check_cancelled()
        return self._runtime_locks is not None or verified_runtime(self.runtime_dir, check=self.check_cancelled)

    def _verified_legacy_runtime(self) -> bool:
        return verified_runtime(self.runtime_dir, legacy=True, check=self.check_cancelled)

    def _copy_protected_frontend(self, directory):
        """Replace only known vendor bytes; the selected model files stay intact."""
        from secure_storage import locked_file
        source = verify_bundled_frontend()
        expected = next(row for row in load_manifest()["files"] if row["path"] == "ollama.exe")
        fd, temporary_name = tempfile.mkstemp(prefix=".frontend-", suffix=".tmp", dir=directory)
        temporary = Path(temporary_name)
        try:
            digest = hashlib.sha256()
            with os.fdopen(fd, "wb") as output, locked_file(source) as input_file:
                while block := input_file.read(TRANSFER_CHUNK_BYTES):
                    self.check_cancelled()
                    output.write(block)
                    digest.update(block)
            if digest.hexdigest() != expected["sha256"] or temporary.stat().st_size != expected["bytes"]:
                raise SetupError(tr('The protected runtime copy failed verification.'))
            temporary.replace(directory / "ollama.exe")
        finally:
            if temporary.exists():
                safe_remove_tree(temporary)

    def _check_disk(self, required: int) -> None:
        self.check_cancelled()
        self.data_root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.data_root).free
        if free < required:
            raise SetupError(tr('This storage drive has {value0:.1f} GB free; setup needs approximately {value1:.1f} GB free. Choose another storage folder or free space, then retry.', value0=free / GIB, value1=required / GIB))

    def _check_setup_child(self, path: Path, *, tree: bool = False) -> None:
        """Verify this exact setup-owned child before a write or removal."""
        if (self.data_root.resolve() != self.data_root or _is_reparse_path(self.data_root)
                or path.parent != self.data_root or path.resolve() != path or _is_reparse_path(path)):
            raise SetupError(tr('The setup temporary path changed or contains a link. No temporary files were removed.'))
        if tree and path.exists():
            # Preflight the whole current-invocation staging tree. Never follow
            # a junction into another folder while cleaning up a failed extract.
            pending = [path]
            while pending:
                directory = pending.pop()
                for child in directory.iterdir():
                    if _is_reparse_path(child) or not child.resolve().is_relative_to(path):
                        raise SetupError(tr('The setup staging folder contains a link. No staging files were removed.'))
                    if child.is_dir():
                        pending.append(child)

    def cleanup_installation(self) -> dict:
        """Remove only the verified runtime archive; retain resumable model data."""
        archive = self.data_root / f"ollama-windows-amd64-{OLLAMA_VERSION}.zip"
        self._check_setup_child(archive)
        if not archive.exists():
            return {"removed": [], "bytes_reclaimed": 0}
        if not self._verified_runtime():
            raise SetupError(tr('The installed runtime could not be verified before cleaning up its download.'))
        if not archive.is_file() or _sha256(archive, self.check_cancelled) != ARCHIVE_SHA256:
            self.progress(tr('Preserved an unverified file at the runtime archive path; it was not treated as a setup download.'))
            return {"removed": [], "bytes_reclaimed": 0}
        size = archive.stat().st_size
        try:
            # Recheck after hashing and before removing this exact file.
            self._check_setup_child(archive)
            archive.unlink()
        except OSError as exc:
            raise SetupError(tr('The runtime is installed, but its temporary download could not be removed. Retry setup to finish cleanup.')) from exc
        self.progress(tr('Removed the verified runtime download archive.'))
        return {"removed": [archive.name], "bytes_reclaimed": size}

    @contextmanager
    def _runtime_response(self, req):
        with self._network_scope(RUNTIME_DOWNLOAD_BUDGET_SECONDS, "The runtime download") as operation:
            opener = request.build_opener(_HttpsRedirect(),
                _ControlledHTTPHandler(lambda: operation), _ControlledHTTPSHandler(lambda: operation))
            with opener.open(req, timeout=min(RUNTIME_DOWNLOAD_IDLE_SECONDS, operation.remaining())) as response:
                yield response

    def _download_runtime(self, archive: Path) -> None:
        """Download within a hard size ceiling, then verify before promotion."""
        self.check_cancelled()
        part = archive.with_suffix(archive.suffix + ".part")
        self._check_setup_child(archive)
        self._check_setup_child(part)
        part_opened = False
        self.progress(tr('Downloading the official portable Ollama runtime (about 1.6 GB)…'))
        deadline = time.monotonic() + RUNTIME_DOWNLOAD_BUDGET_SECONDS
        try:
            req = request.Request(ARCHIVE_URL, headers={"User-Agent": "L2ChatTranslator-Setup/1"})
            with self._runtime_response(req) as source, part.open("wb") as output:
                part_opened = True
                length = source.headers.get("Content-Length")
                if length is not None and (len(length) > 20 or not length.isdecimal()
                                           or not 0 < int(length) <= MAX_RUNTIME_ARCHIVE_BYTES):
                    raise SetupError(tr('The runtime download declared an invalid or oversized length. No runtime was installed.'))
                total = int(length) if length is not None else 0
                downloaded = 0
                last_bucket = -1
                digest = hashlib.sha256()
                while True:
                    self.check_cancelled()
                    if time.monotonic() >= deadline:
                        raise SetupError(tr('The runtime download exceeded its time budget. Retry setup on a faster connection.'))
                    # read1 returns after an underlying read so a slow drip of
                    # bytes cannot hide the overall budget inside read(n).
                    block = getattr(source, "read1", source.read)(TRANSFER_CHUNK_BYTES)
                    if not block:
                        break
                    downloaded += len(block)
                    if downloaded > MAX_RUNTIME_ARCHIVE_BYTES or (total and downloaded > total):
                        raise SetupError(tr('The runtime download exceeded its supported size. No runtime was installed.'))
                    output.write(block)
                    digest.update(block)
                    bucket = int(100 * downloaded / total) // DOWNLOAD_PROGRESS_PERCENT_STEP if total else downloaded // DOWNLOAD_PROGRESS_BYTES
                    if bucket != last_bucket:
                        self.progress(tr('Runtime download: {value0:.0f} MB', value0=downloaded / DECIMAL_MB) + (tr(' / {value0:.0f} MB', value0=total / DECIMAL_MB) if total else ""))
                        last_bucket = bucket
                if total and downloaded != total:
                    raise SetupError(tr('The runtime download was incomplete. Retry setup.'))
                if digest.hexdigest() != ARCHIVE_SHA256:
                    raise SetupError(tr('The runtime download failed SHA-256 verification. No runtime was installed; retry setup.'))
            self.check_cancelled()
            part.replace(archive)
        except (error.URLError, TimeoutError, OSError, HTTPException) as exc:
            raise SetupError(tr('Could not download the runtime: {exc}. Check your connection and retry setup.', exc=exc)) from exc
        finally:
            # Only a partial file opened by this invocation belongs to it.
            # A connection failure before opening must not remove an old file.
            if part_opened and part.is_file():
                self._check_setup_child(part)
                part.unlink()

    def ensure_runtime(self) -> Path:
        """Reuse a verified runtime or stage a verified archive without overwrites."""
        self.check_cancelled()
        self._protect_storage()
        if self._verified_runtime():
            self.progress(tr('Verified protected Ollama {BACKEND_VERSION} is already installed.', BACKEND_VERSION=BACKEND_VERSION))
            if self._runtime_locks is None:
                self._runtime_locks = pin_runtime(self.runtime_dir, check=self.check_cancelled)
            self.cleanup_installation()
            return self.executable
        if self.runtime_dir.exists():
            if not self._verified_legacy_runtime():
                raise SetupError(tr('An existing runtime at {value0} could not be verified against the trusted file inventory. Choose a new empty storage folder or restore the official runtime files. Setup will not overwrite unknown changes.', value0=self.runtime_dir))
            self.progress(tr("Upgrading the verified local runtime's connection security; your models are retained…"))
            self._copy_protected_frontend(self.runtime_dir)
            self._runtime_locks = pin_runtime(self.runtime_dir, check=self.check_cancelled)
            self.cleanup_installation()
            return self.executable
        self._check_disk(RUNTIME_PEAK_BYTES)
        archive = self.data_root / f"ollama-windows-amd64-{OLLAMA_VERSION}.zip"
        self._check_setup_child(archive)
        if not archive.is_file() or _sha256(archive, self.check_cancelled) != ARCHIVE_SHA256:
            self._download_runtime(archive)
        self.progress(tr('Verifying and extracting the runtime and GPU libraries…'))
        if _sha256(archive, self.check_cancelled) != ARCHIVE_SHA256:
            raise SetupError(tr('The runtime archive failed SHA-256 verification.'))
        staging = Path(tempfile.mkdtemp(prefix=".ollama-stage-", dir=self.data_root)).resolve()
        try:
            self._check_setup_child(staging, tree=True)
            _extract_archive(archive, staging, self.check_cancelled)
            executable = staging / "ollama.exe"
            if not executable.is_file():
                raise SetupError(tr('The verified archive did not contain ollama.exe.'))
            with pin_runtime(staging, legacy=True, check=self.check_cancelled):
                pass
            self._copy_protected_frontend(staging)
            receipt = {
                "version": BACKEND_VERSION, "source": ARCHIVE_URL,
                "archiveSha256": ARCHIVE_SHA256, "executableSha256": _sha256(executable, self.check_cancelled),
                "installedAtUtc": datetime.now(timezone.utc).isoformat(),
            }
            (staging / "verified-install.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
            if self.runtime_dir.exists():
                raise SetupError(tr('Another setup created the runtime folder. Retry setup.'))
            if staging.parent != self.data_root or self.runtime_dir.parent != self.data_root:
                raise SetupError(tr('The runtime staging location is invalid.'))
            self.check_cancelled()
            staging.rename(self.runtime_dir)
            self._runtime_locks = pin_runtime(self.runtime_dir, check=self.check_cancelled)
            self.cleanup_installation()
        finally:
            if staging.exists() and staging.parent == self.data_root and staging.name.startswith(".ollama-stage-"):
                self._check_setup_child(staging, tree=True)
                safe_remove_tree(staging)
        return self.executable

    def _port_open(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=1):
                return True
        except OSError:
            return False

    def _verify_owner(self, *, timeout: float = OWNER_CHECK_SECONDS) -> dict:
        """Correlate a held probe socket with its server owner and bind address."""
        self.check_cancelled()
        if os.name != "nt":
            raise SetupError(tr('Portable runtime setup requires Windows 10 or later (64-bit).'))
        deadline = time.monotonic() + timeout
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=min(OWNER_PROBE_SECONDS, timeout)) as probe:
                probe_port = probe.getsockname()[1]
                script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
$connections = @(Get-NetTCPConnection -LocalPort SERVICE_PORT -ErrorAction Stop)
$owners = @($connections | Where-Object { $_.LocalAddress -eq '127.0.0.1' -and $_.RemoteAddress -eq '127.0.0.1' -and $_.RemotePort -eq PROBE_PORT -and $_.OwningProcess -gt 0 } | Select-Object -ExpandProperty OwningProcess -Unique)
if ($owners.Count -ne 1) { throw 'Could not identify the dedicated service owner.' }
$listeners = @($connections | Where-Object { $_.OwningProcess -eq $owners[0] -and $_.RemoteAddress -in @('0.0.0.0', '::') -and $_.RemotePort -eq 0 })
if ($listeners.Count -ne 1 -or $listeners[0].LocalAddress -ne '127.0.0.1') { throw 'The service is not exclusively loopback-bound.' }
$process = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $owners[0]) -ErrorAction Stop
@{ executable = $process.ExecutablePath; address = $listeners[0].LocalAddress; pid = $owners[0] } | ConvertTo-Json -Compress
""".replace("PROBE_PORT", str(int(probe_port))).replace("SERVICE_PORT", str(PORT))
                powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SetupError(tr('The local service owner check timed out.'))
                result = subprocess.run([str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, encoding="utf-8", timeout=remaining, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.check_cancelled()
                if result.returncode != 0:
                    raise SetupError(tr('Windows could not verify the dedicated local service. Close any other translator instance using this port, then retry.'))
                owner = json.loads(result.stdout.lstrip("\ufeff"))
                # Missing identity is not evidence that the service is unrelated.
                # Keep it a verification failure so uninstall cannot delete data
                # while an unidentified process might still be using it.
                if (not isinstance(owner, dict)
                        or not isinstance(owner.get("executable"), str)
                        or not owner["executable"].strip()
                        or not Path(owner["executable"]).is_absolute()
                        or owner.get("address") != "127.0.0.1"
                        or type(owner.get("pid")) is not int or owner["pid"] <= 0):
                    raise SetupError(tr('Windows returned incomplete or unsafe local service ownership information. No data was deleted.'))
                actual = os.path.normcase(str(Path(owner["executable"]).resolve()))
                expected = os.path.normcase(str(self.executable.resolve()))
                if actual != expected:
                    raise ServiceOwnershipMismatch(tr("Port {PORT} is occupied by a different service or storage installation. Close that service before selecting this storage folder. No other process was stopped.", PORT=PORT))
                return owner
        except SetupError:
            raise
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise SetupError(tr('Could not verify the local Ollama process. Retry setup after closing other translator instances.')) from exc

    @contextmanager
    def _open_api(self, path: str, payload: dict | None = None, *, method: str | None = None,
                  timeout: float = API_TIMEOUT_SECONDS, expected_pid: int | None = None,
                  budget_seconds: float | None = None):
        """Authenticate the actual request connection, never only a probe."""
        budget = timeout if budget_seconds is None else budget_seconds
        with self._network_scope(budget, "The local service request") as operation:
            session = get_session(ENDPOINT)
            if expected_pid is not None and session.identity.pid != expected_pid:
                raise SetupError(tr('The dedicated service changed during shutdown. No replacement service was stopped.'))
            body = None if payload is None else json.dumps(payload).encode("utf-8")
            req = request.Request(ENDPOINT + path, data=body, method=method, headers={"Content-Type": "application/json"})
            try:
                with self._opener.open(req, timeout=min(timeout, operation.remaining())) as response:
                    yield response
            except (error.URLError, OSError, TimeoutError) as exc:
                operation.check()
                raise SetupError(tr('The dedicated local service could not complete {path}: {exc}', path=path, exc=exc)) from exc

    def _request_api(self, path: str, payload: dict | None = None, *, method: str | None = None,
                     timeout: float = API_TIMEOUT_SECONDS, expected_pid: int | None = None) -> dict:
        """Read a bounded JSON response so setup errors cannot exhaust memory."""
        try:
            with self._open_api(path, payload, method=method, timeout=timeout, expected_pid=expected_pid) as response:
                data = response.read(MAX_API_RESPONSE_BYTES + 1)
                if len(data) > MAX_API_RESPONSE_BYTES:
                    raise SetupError(tr('The local service returned an oversized response.'))
                result = json.loads(data) if data.strip() else {}
        except (ValueError, UnicodeError) as exc:
            raise SetupError(tr('The local service returned an unreadable response.')) from exc
        except (OSError, HTTPException) as exc:
            raise SetupError(tr('The dedicated local service response was interrupted during {path}: {exc}', path=path, exc=exc)) from exc
        if not isinstance(result, dict) or result.get("error"):
            raise SetupError(tr('The local service reported an error: ') + str(result.get("error", "invalid response") if isinstance(result, dict) else "invalid response"))
        return result

    def ensure_service(self) -> None:
        """Start only this store's service, with cloud and request logging disabled."""
        self.check_cancelled()
        if self._owned_service is not None:
            if self._owned_service.poll() is not None:
                self._owned_service = None
            elif not self._port_open():
                # Do not replace the only reference to a partially started
                # child. Its owner must finish cleanup before another start.
                raise SetupError(tr('The previous local service is still starting or stopping. Finish local service cleanup before retrying.'))
        # Detect another storage installation before downloading a new runtime.
        # A second check below handles a listener appearing during installation.
        if self._port_open():
            self._verify_owner()
            try:
                get_session(ENDPOINT)
            except ServiceIdentityError as exc:
                raise SetupError(tr("A previous local service is still running without this launcher's authentication. Close the old translator, or use Stop local service in Configure, then retry.")) from exc
        self.ensure_runtime()
        if not self._port_open():
            # The listener can disappear while the runtime is being checked.
            # Recheck ownership here too, before replacing the retained handle.
            if self._owned_service is not None:
                if self._owned_service.poll() is None:
                    raise SetupError(tr('The previous local service is still starting or stopping. Finish local service cleanup before retrying.'))
                self._owned_service = None
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            self.models_dir.mkdir(parents=True, exist_ok=True)
            self.service_home.mkdir(parents=True, exist_ok=True)
            self.progress(tr('Starting the dedicated local translation service…'))
            credential = secrets.token_hex(32)
            # Ambient developer settings must not persist chat request bodies or
            # grant arbitrary websites access to the local model. Empty origins
            # retains Ollama's built-in local origins, without inherited extras.
            env = _service_environment(self.models_dir, self.service_home, credential)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            with (self.logs_dir / f"ollama-{stamp}.stdout.log").open("ab") as stdout, (self.logs_dir / f"ollama-{stamp}.stderr.log").open("ab") as stderr:
                self.check_cancelled()
                process = subprocess.Popen([str(self.executable), "serve"], cwd=self.runtime_dir, env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), close_fds=True)
                # Keep ownership even if readiness/version checks fail below.
                self._owned_service = process
                self._session = register_session(ENDPOINT, process, self.executable, credential)
            for _ in range(int(SERVICE_STARTUP_SECONDS / SERVICE_STARTUP_POLL_SECONDS)):
                self.check_cancelled()
                if process.poll() is not None:
                    raise SetupError(tr('The local translation service stopped during startup. See {value0}.', value0=self.logs_dir))
                if self._port_open():
                    break
                if self.cancel_event is None:
                    time.sleep(SERVICE_STARTUP_POLL_SECONDS)
                else:
                    self.cancel_event.wait(SERVICE_STARTUP_POLL_SECONDS)
            else:
                process.terminate()
                raise SetupError(tr('The local service did not start within one minute. See {value0}.', value0=self.logs_dir))
        version = self._request_api("/api/version", timeout=10).get("version")
        if version != BACKEND_VERSION:
            raise SetupError(tr('The dedicated service reports version {version}; expected {BACKEND_VERSION}. Close the old service and retry.', version=version, BACKEND_VERSION=BACKEND_VERSION))

    def stop_owned_service(self) -> None:
        # Cancellation must never prevent teardown of the child it interrupted.
        previous = self._ignore_cancel
        self._ignore_cancel = True
        try:
            self._stop_owned_service()
        finally:
            if self._owned_service is None:
                forget_session(ENDPOINT, self._session)
                self._session = None
                self._release_storage()
            self._ignore_cancel = previous

    def _stop_owned_service(self) -> None:
        """Release only the child this manager started, including partial starts.

        Explicit service-stop commands still use stop_service(). This method
        is for automatic lifecycle cleanup and must leave adopted or replacement
        listeners alone. On Windows, Popen retains the exact child's handle.
        """
        process = self._owned_service
        if process is None:
            return
        if process.poll() is not None:
            self._owned_service = None
            return
        self.progress(tr('Stopping the local service started by this window…'))
        try:
            if self._port_open():
                owner = self._verify_owner(timeout=SHUTDOWN_OWNER_CHECK_SECONDS)
                if owner.get("pid") == process.pid and process.poll() is None:
                    self._unload_running_models(process.pid)
        except Exception:
            # Unloading is best-effort, including a failed readiness check.
            # The retained child handle remains safe even if the listener is
            # missing, replaced, or cannot be verified for API requests.
            self.progress(tr('Model unloading did not finish; stopping only the service started by this window.'))
        try:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=SERVICE_STOP_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as exc:
            # Keep the handle so closing/retrying can finish the same cleanup.
            raise SetupError(tr('The local service started by this window has not stopped. Retry local service cleanup.')) from exc
        self._owned_service = None
        self.progress(tr('The local service started by this window is stopped.'))

    def _terminate_verified_process(self, process_id: int) -> None:
        """Pin a Windows process handle, recheck its image, then terminate it.

        Checking the image on the held handle prevents a reused numeric PID
        from redirecting the stop operation to a different executable.
        """
        if os.name != "nt":
            raise SetupError(tr('Stopping the portable local service requires Windows.'))
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateProcess.restype = wintypes.BOOL
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE | SYNCHRONIZE, False, process_id)
        if not handle:
            raise SetupError(tr('Windows could not open the verified local service for stopping. Retry after closing the translator.'))
        try:
            # Explicit stop/uninstall may encounter a legacy service not started
            # by this invocation. Its path alone is insufficient on shared PCs.
            from local_service import _user_sid
            current = kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, os.getpid())
            if not current:
                raise SetupError(tr('Windows could not verify this account. No process was stopped.'))
            try:
                if _user_sid(handle) != _user_sid(current):
                    raise ServiceOwnershipMismatch(tr("The local service belongs to another Windows account. No process was stopped."))
            finally:
                kernel.CloseHandle(current)
            path = ctypes.create_unicode_buffer(WINDOWS_PATH_BUFFER_CHARS)
            length = wintypes.DWORD(len(path))
            if not kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(length)):
                raise SetupError(tr('Windows could not recheck the local service executable. No process was stopped.'))
            actual = os.path.normcase(str(Path(path.value).resolve()))
            expected = os.path.normcase(str(self.executable.resolve()))
            if actual != expected:
                raise SetupError(tr('The service process changed before it could be stopped. No process was stopped; retry.'))
            if not kernel.TerminateProcess(handle, 0):
                raise SetupError(tr('Windows could not stop the verified local service. Retry after closing the translator.'))
            if kernel.WaitForSingleObject(handle, SERVICE_STOP_SECONDS * 1000) != WAIT_OBJECT_0:
                raise SetupError(tr('The verified local service has not exited yet. Wait a moment and retry.'))
        finally:
            kernel.CloseHandle(handle)

    def _unload_running_models(self, process_id: int) -> None:
        """Ask the verified server to release its runners within a short budget.

        An empty prompt with keep_alive=0 unloads instead of generating text.
        Only models actually reported as loaded are touched; none are deleted.
        """
        deadline = time.monotonic() + MODEL_UNLOAD_BUDGET_SECONDS

        def api(path, payload=None):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SetupError(tr('Model unloading exceeded its shutdown time budget.'))
            return self._request_api(path, payload, timeout=remaining, expected_pid=process_id)

        entries = api("/api/ps").get("models", [])
        if not isinstance(entries, list):
            raise SetupError(tr('The local service returned an invalid loaded-model list.'))
        names = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("remote_host") or entry.get("remote_model"):
                continue
            try:
                tag = _model_tag(entry.get("name", entry.get("model", "")))
            except SetupError:
                continue
            if tag not in names:
                names.append(tag)
        # The dedicated server is configured for one loaded model. Keep the
        # shutdown bounded even if a different local caller changed that limit.
        if len(names) > MAX_MODELS_TO_UNLOAD:
            raise SetupError(tr('Too many models were loaded for a bounded shutdown.'))
        for tag in names:
            api("/api/generate", {"model": tag, "prompt": "", "keep_alive": 0, "stream": False})

    def stop_service(self) -> None:
        previous = self._ignore_cancel
        self._ignore_cancel = True
        try:
            self._stop_service()
        finally:
            self._ignore_cancel = previous

    def _stop_service(self) -> None:
        """Unload models, then stop only this saved storage installation.

        Unloading is best-effort if inference is busy. This never installs,
        starts, deletes models, or kills other Ollama installations by name.
        """
        if not self._port_open():
            self.progress(tr('The dedicated local service is already stopped.'))
            return
        if not self._verified_runtime() and not self._verified_legacy_runtime():
            raise SetupError(tr('The saved runtime could not be verified. No process was stopped.'))
        owner = self._verify_owner(timeout=SHUTDOWN_OWNER_CHECK_SECONDS)
        process_id = owner.get("pid")
        if isinstance(process_id, bool) or not isinstance(process_id, int) or process_id <= 0:
            raise SetupError(tr('Windows returned an invalid service process identity. No process was stopped.'))
        self.progress(tr("Stopping this installation's dedicated local service…"))
        try:
            self._unload_running_models(process_id)
        except SetupError:
            self.progress(tr('Model unloading did not finish; stopping the verified service. GPU memory release could not be confirmed.'))
        if not self._port_open():
            self.progress(tr('The dedicated local service is already stopped.'))
            return
        current = self._verify_owner(timeout=SHUTDOWN_OWNER_CHECK_SECONDS)
        if current.get("pid") != process_id:
            raise SetupError(tr('The dedicated service changed during shutdown. No replacement service was stopped.'))
        self._terminate_verified_process(process_id)
        for _ in range(int(SERVICE_STOP_SECONDS / SERVICE_STOP_POLL_SECONDS)):
            if not self._port_open():
                self.progress(tr('The dedicated local service is stopped. You can choose another storage folder.'))
                return
            time.sleep(SERVICE_STOP_POLL_SECONDS)
        raise SetupError(tr('Port {PORT} is still occupied after stopping this service. No additional process was stopped; close the other service before changing storage.', PORT=PORT))

    def installed_models(self) -> list[dict]:
        """List valid local tags, excluding remote aliases and malformed entries."""
        entries = self._request_api("/api/tags").get("models", [])
        if not isinstance(entries, list):
            raise SetupError(tr('The local service returned an invalid model list.'))
        result = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("remote_host") or entry.get("remote_model"):
                continue
            name = entry.get("name", entry.get("model", ""))
            try:
                _model_tag(name)
            except SetupError:
                continue
            result.append(dict(entry, name=name))
        return result

    def install_model(self, tag: str) -> None:
        """Pull one catalog model and verify its local presence before reporting ready."""
        with self._network_scope(MODEL_PULL_BUDGET_SECONDS, "The model download"):
            self._install_model(tag)

    def _install_model(self, tag: str) -> None:
        self.check_cancelled()
        deadline = time.monotonic() + MODEL_PULL_BUDGET_SECONDS
        if tag not in MODEL_CATALOG:
            raise SetupError(tr('Choose a supported model from the model list.'))
        if any(entry["name"] == tag for entry in self.installed_models()):
            self.progress(tr('{tag} is already installed.', tag=tag))
            self._verify_local_model(tag)
            return
        self._check_disk(MODEL_CATALOG[tag]["download_bytes"] + MODEL_FREE_SPACE_MARGIN_BYTES)
        self.progress(tr('Downloading {tag} (about {value1:.1f} GB)…', tag=tag, value1=MODEL_CATALOG[tag]['download_gb']))
        success = False
        last_update = None
        try:
            with self._open_api("/api/pull", {"model": tag, "stream": True}, timeout=MODEL_PULL_IDLE_SECONDS,
                                budget_seconds=deadline - time.monotonic()) as response:
                while True:
                    self.check_cancelled()
                    if time.monotonic() >= deadline:
                        raise SetupError(tr('The model download exceeded its time budget. Retry setup to resume downloaded model layers.'))
                    line = response.readline(MAX_PULL_STATUS_BYTES + 1)
                    self.check_cancelled()
                    if not line:
                        break
                    if len(line) > MAX_PULL_STATUS_BYTES:
                        raise SetupError(tr('The local model download returned an oversized status response.'))
                    update = json.loads(line)
                    if not isinstance(update, dict) or update.get("error"):
                        raise SetupError(tr('Model download failed: ') + str(update.get("error", "invalid response") if isinstance(update, dict) else "invalid response"))
                    if update.get("total", 0) > 0:
                        percent = min(100, max(0, int(100 * update.get("completed", 0) / update["total"])))
                        status = (update.get("digest"), percent)
                        if status != last_update:
                            self.progress(tr('Model download: {percent}% ({value1:.0f}/{value2:.0f} MB)', percent=percent, value1=update.get('completed', 0) / DECIMAL_MB, value2=update['total'] / DECIMAL_MB))
                            last_update = status
                    elif update.get("status"):
                        if update["status"] != last_update:
                            self.progress(tr(str(update["status"])))
                            last_update = update["status"]
                    success = success or update.get("status") == "success"
        except (ValueError, UnicodeError, OSError, TimeoutError) as exc:
            raise SetupError(tr('Model download was interrupted: {exc}. Retry setup to resume the download.', exc=exc)) from exc
        if not success:
            raise SetupError(tr('Model download ended before completion. Retry setup to resume it.'))
        if not any(entry["name"] == tag for entry in self.installed_models()):
            raise SetupError(tr('The selected model was not found after download. Retry setup.'))
        self._verify_local_model(tag)
        self.check_cancelled()

    def _verify_local_model(self, tag: str) -> None:
        details = self._request_api("/api/show", {"model": _model_tag(tag)}, timeout=30)
        if details.get("remote_host") or details.get("remote_model"):
            raise SetupError(tr('The selected model routes to a remote service. Choose a downloaded local model.'))

    def remove_model(self, tag: str) -> None:
        """Delete only the selected installed tag through the verified local API."""
        self.check_cancelled()
        _model_tag(tag)
        if not any(entry["name"] == tag for entry in self.installed_models()):
            raise SetupError(tr('That model is not installed in this storage folder.'))
        self.progress(tr('Removing {tag} from this installation…', tag=tag))
        self._request_api("/api/delete", {"model": tag}, method="DELETE", timeout=60)
        if any(entry["name"] == tag for entry in self.installed_models()):
            raise SetupError(tr('The model is still listed after removal. Retry the removal.'))

    def prepare(self, tag: str = DEFAULT_MODEL) -> None:
        self.check_cancelled()
        if tag not in MODEL_CATALOG:
            raise SetupError(tr('Choose a supported model from the model list.'))
        self._protect_storage()
        if not self._verified_runtime():
            self._check_disk(required_disk_bytes(tag))
        self.ensure_service()
        self.install_model(tag)
        self.progress(tr('Ready: {tag}. Translation stays on this computer.', tag=tag))
