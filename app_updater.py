"""Explicit, verified updates from the application's fixed HTTPS release host.

Checking never downloads or executes an installer. Installing is a separate
user action and is handed to an external runner only after clean shutdown.
Update requests contain the app version, never chat, model prompts or paths.
HTTPS authenticates the release host; its checksum is not a separate signature.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import stat
import subprocess
import sys
import time
from urllib import error, request
import uuid

from app_paths import preferences_directory
from app_version import VERSION

BASE_URL = 'https://40.160.140.227'
RELEASE_URL = BASE_URL + '/downloads/release.json'
PRODUCT = 'Lineage 2 Chat Translator'
APP_ID = 'Lineage2ChatTranslator'
UPDATE_MUTEX = r'Local\Lineage2ChatTranslatorUpdate'
INSTALL_REGISTRY_KEY = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\{1D8CE3C7-53A6-4C67-AC9F-1C6F0D6B1920}_is1'
RUNNER_PATH = Path(__file__).resolve().parent / 'assets' / 'apply-update.ps1'
# Resource limits bound untrusted release responses and local handoff records.
MAX_METADATA_BYTES = 64 * 1024
MAX_SMALL_RECORD_BYTES = 4 * 1024
MAX_RESULT_BYTES = 16 * 1024
MAX_RESULT_MESSAGE_CHARS = 8000
MIN_INSTALLER_BYTES = 1024
MAX_INSTALLER_BYTES = 128 * 1024 * 1024
MIN_RUNNER_BYTES = 1000
MAX_RUNNER_BYTES = 128 * 1024
METADATA_CHUNK_BYTES = 16 * 1024
DOWNLOAD_CHUNK_BYTES = 256 * 1024
REQUEST_TIMEOUT_SECONDS = 20
MAX_METADATA_SECONDS = 30.0
MAX_DOWNLOAD_SECONDS = 300.0
READY_TIMEOUT = 15.0
HANDOFF_POLL_SECONDS = 0.05
CHILD_STOP_TIMEOUT_SECONDS = 3
STAGE_PREFIX = 'update-'
# Win32 constants are fixed API values, not adjustable application settings.
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
SYNCHRONIZE_ACCESS = 0x00100000
ERROR_ACCESS_DENIED = 5
WINDOWS_PATH_BUFFER_CHARS = 32768
_VERSION = re.compile(r'(?:0|[1-9][0-9]{0,4})\.(?:0|[1-9][0-9]{0,4})\.(?:0|[1-9][0-9]{0,4})\Z')
_STAGE = re.compile(r'update-[a-f0-9]{32}\Z')
_FILES = frozenset({'owner.json', 'job.json', 'installer.part', 'installer.exe',
                    'apply-update.ps1', 'ready.json', 'commit.json', 'commit.pending', 'cancel',
                    'self-test.json', 'installer.log'})


class UpdateError(RuntimeError):
    pass


class UpdateCancelled(UpdateError):
    pass


@dataclass(frozen=True)
class UpdateRelease:
    version: str
    file: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class PreparedUpdate:
    release: UpdateRelease
    stage: Path


def _require(condition, message):
    if not condition:
        raise UpdateError(message)


def _version(value):
    _require(isinstance(value, str) and _VERSION.fullmatch(value), 'The update version is invalid.')
    return tuple(map(int, value.split('.')))


def _validate_release(release, current=VERSION):
    _require(isinstance(release, UpdateRelease), 'The update details are invalid.')
    _require(_version(release.version) > _version(current), 'This release is not newer than the installed version.')
    _require(release.file == f'Lineage-2-Chat-Translator-{release.version}-Setup.exe',
             'The update installer filename is invalid.')
    _require(type(release.bytes) is int and MIN_INSTALLER_BYTES <= release.bytes <= MAX_INSTALLER_BYTES,
             'The update installer size is invalid.')
    _require(isinstance(release.sha256, str) and re.fullmatch(r'[a-f0-9]{64}', release.sha256),
             'The update checksum is invalid.')
    return release


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, 'The update record contains duplicate fields.')
        result[key] = value
    return result


def _json_bytes(data):
    try:
        value = json.loads(data.decode('utf-8-sig'), object_pairs_hook=_unique)
    except (ValueError, UnicodeError) as exc:
        raise UpdateError('The update information is unreadable.') from exc
    _require(isinstance(value, dict), 'The update information is invalid.')
    return value


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise UpdateError('The update server redirected the request. No update was downloaded.')


def _opener():
    """Use OS certificate trust, bypass proxies, and reject every redirect."""
    return request.build_opener(request.ProxyHandler({}),
                                request.HTTPSHandler(context=ssl.create_default_context()), _NoRedirect())


def _open(url):
    _require(url == RELEASE_URL or re.fullmatch(
        re.escape(BASE_URL) + r'/downloads/Lineage-2-Chat-Translator-[0-9]+\.[0-9]+\.[0-9]+-Setup\.exe', url),
        'Only the official HTTPS release host is allowed.')
    try:
        response = _opener().open(request.Request(url, headers={
            'Accept': 'application/json' if url == RELEASE_URL else 'application/octet-stream',
            'User-Agent': f'Lineage2ChatTranslator/{VERSION}'}), timeout=REQUEST_TIMEOUT_SECONDS)
        if response.status != 200 or response.geturl() != url:
            response.close()
            raise UpdateError('The update server returned an unexpected response.')
        return response
    except (error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError('Could not reach the update server. Check your internet connection and try again.') from exc


def check_for_update(current_version=VERSION):
    """Fetch only release metadata; return a validated newer release or None."""
    _version(current_version)
    with _open(RELEASE_URL) as response:
        content = bytearray()
        deadline = time.monotonic() + MAX_METADATA_SECONDS
        reader = getattr(response, 'read1', response.read)
        while len(content) <= MAX_METADATA_BYTES:
            _require(time.monotonic() < deadline, 'The update check took too long. Try again later.')
            block = reader(min(METADATA_CHUNK_BYTES, MAX_METADATA_BYTES + 1 - len(content)))
            if not block:
                break
            content.extend(block)
    _require(len(content) <= MAX_METADATA_BYTES, 'The update information is too large.')
    document = _json_bytes(bytes(content))
    _require(document.get('product') == PRODUCT and document.get('platform') == 'windows-x64',
             'The update does not belong to this application.')
    _require(isinstance(document.get('validation'), dict)
             and document['validation'].get('packaged_self_test_passed') is True,
             'The release has not passed its packaged checks.')
    version = document.get('version')
    candidate = _version(version)
    artifact = document.get('installer')
    _require(isinstance(artifact, dict), 'The update installer information is missing.')
    release = UpdateRelease(version, artifact.get('file'), artifact.get('bytes'), artifact.get('sha256'))
    # Validate even non-new metadata without accepting it as an install action.
    _require(release.file == f'Lineage-2-Chat-Translator-{version}-Setup.exe'
             and type(release.bytes) is int and MIN_INSTALLER_BYTES <= release.bytes <= MAX_INSTALLER_BYTES
             and isinstance(release.sha256, str) and re.fullmatch(r'[a-f0-9]{64}', release.sha256),
             'The update installer information is invalid.')
    return _validate_release(release, current_version) if candidate > _version(current_version) else None


def _plain_path(path):
    """Reject existing links/junctions at every level before using a local path."""
    value = Path(path)
    _require(value.is_absolute(), 'The update location must be an absolute path.')
    value = Path(os.path.abspath(value))
    for component in reversed((value, *value.parents)):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        _require(not stat.S_ISLNK(info.st_mode) and not (getattr(info, 'st_file_attributes', 0) & FILE_ATTRIBUTE_REPARSE_POINT),
                 'Updates cannot use linked or redirected folders.')
    return value.resolve()


def _preferences(preferences=None):
    path = _plain_path(preferences if preferences is not None else preferences_directory())
    _require(path.name.casefold() == 'lineage2chattranslator'
             or (path.name.casefold() == 'chattranslator' and path.parent.name.casefold() == 'l2prominence'),
             'The update preferences folder is invalid.')
    return path


def _installed_executable():
    _require(os.name == 'nt' and getattr(sys, 'frozen', False),
             'Automatic installation is available in the installed Windows app. Download the installer from the website for this source copy.')
    executable = _plain_path(sys.executable)
    _require(executable.name.casefold() == 'lineage2chattranslator.exe' and executable.is_file(),
             'The installed launcher could not be identified.')
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INSTALL_REGISTRY_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            directory = winreg.QueryValueEx(key, 'InstallLocation')[0]
            display_name = winreg.QueryValueEx(key, 'DisplayName')[0]
    except OSError as exc:
        raise UpdateError('This copy is not registered as an installed translator. Use the website installer first.') from exc
    _require(isinstance(display_name, str) and (display_name == PRODUCT or display_name.startswith(PRODUCT + ' ')),
             'The installed application identity does not match.')
    _require(_plain_path(directory) == executable.parent and _plain_path(executable.parent / 'unins000.exe').is_file(),
             'The installed application folder does not match its registration.')
    return executable


def installation_support():
    """Explain whether this executable is the registered installed application."""
    try:
        _installed_executable()
        return True, ''
    except (UpdateError, OSError, ValueError) as exc:
        return False, str(exc)


def _read_json(path, maximum=MAX_METADATA_BYTES):
    """Bound the read itself: a file can grow after the preliminary size check."""
    path = _plain_path(path)
    _require(path.is_file() and path.stat().st_size <= maximum, 'The update record is missing or too large.')
    with path.open('rb') as source:
        content = source.read(maximum + 1)
    _require(len(content) <= maximum, 'The update record is too large.')
    return _json_bytes(content)


def _write_new_json(path, document):
    path = _plain_path(path)
    with path.open('x', encoding='utf-8') as output:
        json.dump(document, output, ensure_ascii=True)


def _stage_info(stage, preferences=None):
    root = _plain_path(_preferences(preferences) / 'updates')
    stage = _plain_path(stage)
    _require(stage.parent == root and _STAGE.fullmatch(stage.name) and stage.is_dir(),
             'The temporary update folder is invalid.')
    owner = _read_json(stage / 'owner.json', MAX_SMALL_RECORD_BYTES)
    _require(owner.get('app_id') == APP_ID and owner.get('schema_version') == 1
             and owner.get('nonce') == stage.name[len(STAGE_PREFIX):], 'The temporary update ownership record does not match.')
    return stage, owner


def _remove_stage(stage, preferences=None):
    stage, _ = _stage_info(stage, preferences)
    children = list(stage.iterdir())
    for child in children:
        _require(child.name in _FILES and _plain_path(child).is_file(),
                 'Unrecognized files in the update folder were left untouched.')
    # Only direct, explicitly owned files are removed. Never recursively delete.
    for child in children:
        if child.name != 'owner.json':
            _plain_path(child).unlink()
    (stage / 'owner.json').unlink()
    try:
        stage.rmdir()
    except OSError:
        # A transient directory handle must not turn an owned empty stage into
        # an unidentifiable leftover which future cleanup cannot retry.
        if _plain_path(stage).is_dir() and not (stage / 'owner.json').exists():
            _write_new_json(stage / 'owner.json', {'app_id': APP_ID, 'schema_version': 1,
                                                  'nonce': stage.name[len(STAGE_PREFIX):]})
        raise


def _cleanup_update_cache(preferences=None, *, strict=True):
    _require(not update_in_progress(), 'An update is still running. Wait for it to finish before cleanup.')
    preferences = _preferences(preferences)
    root = _plain_path(preferences / 'updates')
    if not root.exists():
        return
    _require(root.is_dir(), 'The update cache is not a folder.')
    failures = []
    for child in root.iterdir():
        try:
            _remove_stage(child, preferences)
        except (UpdateError, OSError) as exc:
            failures.append(str(exc))
    if not any(root.iterdir()):
        root.rmdir()
    if strict and failures:
        raise UpdateError(failures[0])


def cleanup_update_cache(preferences=None, *, strict=True):
    """Remove only owned direct files; best-effort startup leaves failures alone."""
    try:
        _cleanup_update_cache(preferences, strict=strict)
    except (UpdateError, OSError, ValueError, TypeError):
        if strict:
            raise
        # Ancillary stale downloads must never prevent normal app startup.


def discard_update(prepared):
    """Discard a canceled download without touching installed files or models."""
    _require(isinstance(prepared, PreparedUpdate), 'The prepared update is invalid.')
    if prepared.stage.exists():
        _remove_stage(prepared.stage)


def _cancelled(event):
    if event is not None and event.is_set():
        raise UpdateCancelled('Update canceled. The installed translator was not changed.')


def _digest(path):
    with _plain_path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def prepare_update(release, *, progress=None, cancelled=None):
    """Download and verify an update, but leave all execution to launch_update."""
    release = _validate_release(release)
    executable = _installed_executable()
    _require(not update_in_progress(), 'Another update is already in progress.')
    _cancelled(cancelled)
    preferences = _preferences()
    preferences.mkdir(parents=True, exist_ok=True)
    cleanup_update_cache(preferences, strict=False)
    root = _plain_path(preferences / 'updates')
    root.mkdir(exist_ok=True)
    nonce = uuid.uuid4().hex
    stage = root / (STAGE_PREFIX + nonce)
    stage.mkdir(exist_ok=False)
    _write_new_json(stage / 'owner.json', {'app_id': APP_ID, 'schema_version': 1, 'nonce': nonce})
    prepared = PreparedUpdate(release, stage)
    try:
        part = stage / 'installer.part'
        received, sha = 0, hashlib.sha256()
        deadline = time.monotonic() + MAX_DOWNLOAD_SECONDS
        with _open(BASE_URL + '/downloads/' + release.file) as response, part.open('xb') as output:
            length = response.headers.get('Content-Length')
            if length is not None:
                _require(length.isdecimal() and int(length) == release.bytes, 'The installer download has an unexpected size.')
            while True:
                _cancelled(cancelled)
                _require(time.monotonic() < deadline, 'The update download took too long. Try again later.')
                block = getattr(response, 'read1', response.read)(DOWNLOAD_CHUNK_BYTES)
                if not block:
                    break
                received += len(block)
                _require(received <= release.bytes, 'The installer download exceeded its expected size.')
                output.write(block)
                sha.update(block)
                if progress:
                    progress(received, release.bytes)
        _cancelled(cancelled)
        _require(received == release.bytes and sha.hexdigest() == release.sha256,
                 'The installer download did not match the published checksum. It was not run.')
        with part.open('rb') as source:
            _require(source.read(2) == b'MZ', 'The download is not a Windows installer.')
        part.replace(stage / 'installer.exe')
        runner = RUNNER_PATH.read_bytes()
        _require(MIN_RUNNER_BYTES <= len(runner) <= MAX_RUNNER_BYTES, 'The bundled update helper is unavailable.')
        (stage / 'apply-update.ps1').write_bytes(runner)
        _write_new_json(stage / 'job.json', {
            'app_id': APP_ID, 'schema_version': 1, 'nonce': nonce,
            'parent_pid': os.getpid(), 'from_version': VERSION,
            'version': release.version, 'installer_file': release.file,
            'installer_bytes': release.bytes, 'installer_sha256': release.sha256,
            'runner_sha256': hashlib.sha256(runner).hexdigest(),
            'executable': str(executable), 'preferences': str(preferences),
        })
        _cancelled(cancelled)
        return prepared
    except BaseException:
        try:
            discard_update(prepared)
        except (UpdateError, OSError):
            pass  # A changed/unrecognized folder is never deleted to hide an error.
        raise


def update_in_progress():
    """Probe the helper's named guard without acquiring or extending its lock."""
    if os.name != 'nt':
        return False
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenMutexW.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.OpenMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenMutexW(SYNCHRONIZE_ACCESS, False, UPDATE_MUTEX)
    if handle:
        kernel.CloseHandle(handle)
        return True
    # Access denied still means an existing update guard, not absence.
    return ctypes.get_last_error() == ERROR_ACCESS_DENIED


def _powershell():
    """Resolve Windows' own PowerShell instead of trusting PATH or the cwd."""
    _require(os.name == 'nt', 'Automatic installation requires Windows.')
    buffer = ctypes.create_unicode_buffer(WINDOWS_PATH_BUFFER_CHARS)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetSystemDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    kernel.GetSystemDirectoryW.restype = ctypes.c_uint32
    length = kernel.GetSystemDirectoryW(buffer, len(buffer))
    _require(0 < length < len(buffer), 'Windows PowerShell could not be located.')
    executable = _plain_path(Path(buffer.value) / 'WindowsPowerShell/v1.0/powershell.exe')
    _require(executable.is_file(), 'Windows PowerShell is unavailable.')
    return executable


def launch_update(prepared):
    """Start the waiting helper, then commit only after its verified handshake.

    The caller must finish shutdown and exit after this returns. The helper
    cannot run the installer until that exact process exits.
    """
    _require(isinstance(prepared, PreparedUpdate), 'The prepared update is invalid.')
    release = _validate_release(prepared.release)
    stage, owner = _stage_info(prepared.stage)
    job = _read_json(stage / 'job.json')
    executable = _installed_executable()
    _require(job.get('app_id') == APP_ID and job.get('schema_version') == 1
             and job.get('nonce') == owner['nonce'] and job.get('parent_pid') == os.getpid()
             and job.get('from_version') == VERSION and job.get('version') == release.version
             and job.get('installer_file') == release.file and job.get('installer_bytes') == release.bytes
             and job.get('installer_sha256') == release.sha256
             and job.get('executable') == str(executable) and job.get('preferences') == str(_preferences()),
             'The update handoff record changed. The installer was not started.')
    _require((stage / 'installer.exe').stat().st_size == release.bytes
             and _digest(stage / 'installer.exe') == release.sha256,
             'The downloaded installer changed. It was not run.')
    runner_hash = _digest(RUNNER_PATH)
    _require(job.get('runner_sha256') == runner_hash and _digest(stage / 'apply-update.ps1') == runner_hash,
             'The update helper changed. It was not run.')
    _require(not any((stage / name).exists() for name in ('ready.json', 'commit.json', 'cancel')),
             'This update handoff was already attempted. Download the update again.')
    process = None
    try:
        process = subprocess.Popen([str(_powershell()), '-NoLogo', '-NoProfile', '-NonInteractive',
                                    '-ExecutionPolicy', 'Bypass', '-File', str(stage / 'apply-update.ps1'),
                                    '-JobPath', str(stage / 'job.json')], cwd=_preferences(),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), close_fds=True)
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise UpdateError('The update helper could not start. Reopen the translator and try again.')
            if (stage / 'ready.json').exists():
                try:
                    ready = _read_json(stage / 'ready.json', MAX_SMALL_RECORD_BYTES)
                except (UpdateError, OSError):
                    time.sleep(HANDOFF_POLL_SECONDS)
                    continue  # The helper may still be finishing its small write.
                _require(ready.get('nonce') == owner['nonce'] and ready.get('pid') == process.pid,
                         'The update helper did not identify itself correctly.')
                pending = _plain_path(stage / 'commit.pending')
                with pending.open('x', encoding='utf-8') as output:
                    json.dump({'nonce': owner['nonce']}, output)
                    output.flush()
                    os.fsync(output.fileno())
                # The runner can only see a complete confirmation, never an
                # empty/partial JSON file while its writer is still active.
                pending.replace(_plain_path(stage / 'commit.json'))
                return
            time.sleep(HANDOFF_POLL_SECONDS)
        raise UpdateError('The update helper did not become ready. Reopen the translator and try again.')
    except BaseException:
        # Terminate only the child we just created, before it has permission to
        # install anything. A late readiness response cannot turn into an update.
        try:
            (stage / 'cancel').touch(exist_ok=True)
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=CHILD_STOP_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=CHILD_STOP_TIMEOUT_SECONDS)
        finally:
            try:
                discard_update(prepared)
            except (UpdateError, OSError):
                pass
        raise


def consume_update_result(preferences=None):
    """Read and remove one local status receipt without making startup depend on it."""
    path = None
    try:
        path = _plain_path(_preferences(preferences) / 'update-result.json')
        if not path.exists():
            return None
        document = _read_json(path, MAX_RESULT_BYTES)
        _require(document.get('app_id') == APP_ID and document.get('status') in ('success', 'error')
                 and isinstance(document.get('message'), str) and len(document['message']) <= MAX_RESULT_MESSAGE_CHARS,
                 'The saved update result is invalid.')
        _version(document.get('version'))
        if document['status'] == 'error' and document['version'] == VERSION:
            # An older update runner can report failure after the new version
            # is already installed (for example, locked retired files). Settings
            # has no newer release to offer then; retry with the same installer.
            document['message'] = document['message'].replace(
                'The installed translator will reopen; you can retry the update from Settings.',
                'The installed translator will reopen. To retry installation or cleanup, '
                'download and rerun this version\'s installer from ' + BASE_URL + '/.')
        path.unlink()
        return {key: document[key] for key in ('status', 'message', 'version')}
    except (UpdateError, OSError, ValueError, TypeError):
        # This is an informational receipt, never a startup requirement.
        # Remove only the exact normal receipt file; never follow a link or
        # remove a folder if it was replaced with something unexpected.
        if path is not None:
            try:
                if _plain_path(path).is_file():
                    path.unlink()
            except (UpdateError, OSError):
                pass
        return {'status': 'error', 'version': VERSION,
                'message': 'The previous update result could not be read. The translator can still start; check its version in Settings before retrying an update.'}
