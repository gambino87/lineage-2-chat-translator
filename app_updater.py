"""Explicit, verified updates from the application's fixed HTTPS release host.

Checking never downloads or executes an installer. Installing is a separate
user action and is handed to an external runner only after clean shutdown.
Update requests contain the app version, never chat, model prompts or paths.
The app's pinned public key authenticates release metadata independently of the
host. A checksum is trusted only after that signature has been verified.
"""
from __future__ import annotations

import ctypes
from contextlib import contextmanager
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
from update_trust import SignatureError, SIGNATURE_BYTES, verify_release_signature
from update_network import ControlledHTTPSHandler, NetworkCancelled, NetworkDeadline, NetworkOperation
from secure_storage import guard_private_tree, locked_file as _locked_helper, path_identity, safe_remove_tree
import incremental_update as file_update

BASE_URL = 'https://40.160.140.227'
RELEASE_URL = BASE_URL + '/downloads/release.json'
SIGNATURE_URL = BASE_URL + '/downloads/release.sig'
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
MAX_JOURNAL_BYTES = 16 * 1024 * 1024
INSTALLED_RECEIPTS = ('installed-files.json', 'installed-release.json', 'installed-release.sig')
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
                    'release.json', 'release.sig',
                    'apply-update.ps1', 'ready.json', 'commit.json', 'commit.pending', 'cancel',
                    'self-test.json', 'installer.log', 'files.json', 'blob.part',
                    'transaction.json', 'transaction.pending', 'receipt.pending'})


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
    # Keep the exact signed bytes, not a reserialized dictionary. Every trust
    # boundary re-verifies them; constructing this dataclass conveys no trust.
    metadata: bytes = b''
    signature: bytes = b''
    incremental: dict | None = None


@dataclass(frozen=True)
class PreparedUpdate:
    release: UpdateRelease
    stage: Path
    mode: str = 'installer'


def _require(condition, message):
    if not condition:
        raise UpdateError(message)


def _version(value):
    _require(isinstance(value, str) and _VERSION.fullmatch(value), 'The update version is invalid.')
    return tuple(map(int, value.split('.')))


def _validate_release(release, current=VERSION):
    _require(isinstance(release, UpdateRelease), 'The update details are invalid.')
    _require(release == _signed_release(release.metadata, release.signature),
             'The update details do not match their signed release. The installer was not run.')
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


def _opener(operation=None):
    """Use OS certificate trust, bypass proxies, and reject every redirect."""
    context = ssl.create_default_context()
    handler = (ControlledHTTPSHandler(operation, context=context) if operation is not None
               else request.HTTPSHandler(context=context))
    return request.build_opener(request.ProxyHandler({}), handler, _NoRedirect())


def _open(url, operation=None):
    _require(url in (RELEASE_URL, SIGNATURE_URL) or re.fullmatch(
        re.escape(BASE_URL) + r'/downloads/Lineage-2-Chat-Translator-[0-9]+\.[0-9]+\.[0-9]+-Setup\.exe', url)
        or re.fullmatch(re.escape(BASE_URL) + r'/updates/(?:[0-9]+\.[0-9]+\.[0-9]+/files\.json|blobs/[a-f0-9]{64}\.gz)', url),
        'Only the official HTTPS release host is allowed.')
    try:
        response = _opener(operation).open(request.Request(url, headers={
            'Accept': 'application/json' if url == RELEASE_URL else 'application/octet-stream',
            'User-Agent': f'Lineage2ChatTranslator/{VERSION}'}), timeout=REQUEST_TIMEOUT_SECONDS)
        if response.status != 200 or response.geturl() != url:
            response.close()
            raise UpdateError('The update server returned an unexpected response.')
        return response
    except (error.URLError, TimeoutError, OSError) as exc:
        if operation is not None:
            operation.check()
        raise UpdateError('Could not reach the update server. Check your internet connection and try again.') from exc


@contextmanager
def _network_scope(seconds, cancelled=None):
    """Translate transport cancellation/deadlines into the public UI errors."""
    try:
        with NetworkOperation(seconds, cancelled) as operation:
            yield operation
    except NetworkCancelled as exc:
        raise UpdateCancelled('Update canceled. The installed translator was not changed.') from exc
    except NetworkDeadline as exc:
        raise UpdateError('The update request took too long. Try again later.') from exc
    except (error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError('Could not reach the update server. Check your internet connection and try again.') from exc


def _fetch_record(url, maximum, operation):
    with _open(url, operation) as response:
        content = bytearray()
        reader = getattr(response, 'read1', response.read)
        while len(content) <= maximum:
            operation.check()
            block = reader(min(METADATA_CHUNK_BYTES, maximum + 1 - len(content)))
            if not block:
                break
            content.extend(block)
    _require(len(content) <= maximum, 'The update information is too large.')
    return bytes(content)


def _signed_release(metadata, signature):
    """Authenticate before interpreting anything supplied by the download host."""
    try:
        verify_release_signature(metadata, signature)
    except SignatureError as exc:
        raise UpdateError('The update signature could not be verified. No installer was run. '
                          'Try again later or check the project website.') from exc
    document = _json_bytes(metadata)
    _require(document.get('product') == PRODUCT and document.get('platform') == 'windows-x64',
             'The update does not belong to this application.')
    _require(isinstance(document.get('validation'), dict)
             and document['validation'].get('packaged_self_test_passed') is True,
             'The release has not passed its packaged checks.')
    version = document.get('version')
    _version(version)
    artifact = document.get('installer')
    _require(isinstance(artifact, dict), 'The update installer information is missing.')
    release = UpdateRelease(version, artifact.get('file'), artifact.get('bytes'), artifact.get('sha256'),
                            metadata, signature, document.get('incremental'))
    # Validate even non-new metadata without accepting it as an install action.
    _require(release.file == f'Lineage-2-Chat-Translator-{version}-Setup.exe'
             and type(release.bytes) is int and MIN_INSTALLER_BYTES <= release.bytes <= MAX_INSTALLER_BYTES
             and isinstance(release.sha256, str) and re.fullmatch(r'[a-f0-9]{64}', release.sha256),
             'The update installer information is invalid.')
    return release


def check_for_update(current_version=VERSION):
    """Fetch signed release details only; unsigned metadata never offers an update."""
    _version(current_version)
    with _network_scope(MAX_METADATA_SECONDS) as operation:
        metadata = _fetch_record(RELEASE_URL, MAX_METADATA_BYTES, operation)
        signature = _fetch_record(SIGNATURE_URL, SIGNATURE_BYTES, operation)
    release = _signed_release(metadata, signature)
    return _validate_release(release, current_version) if _version(release.version) > _version(current_version) else None


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


def _read_bytes(path, maximum=MAX_METADATA_BYTES):
    """Bound the read itself: a file can grow after the preliminary size check."""
    path = _plain_path(path)
    _require(path.is_file() and path.stat().st_size <= maximum, 'The update record is missing or too large.')
    with path.open('rb') as source:
        content = source.read(maximum + 1)
    _require(len(content) <= maximum, 'The update record is too large.')
    return content


def _read_json(path, maximum=MAX_METADATA_BYTES):
    return _json_bytes(_read_bytes(path, maximum))


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
    _require(not _incomplete_transaction(stage),
             'An interrupted file update has recovery backups. Run the full website installer to repair it; backups were kept.')
    children = list(stage.iterdir())
    for child in children:
        _require(_stage_filename(child.name) and _plain_path(child).is_file(),
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


def _authorize_update_cache(root):
    """Only repair ACLs for a cache entirely described by our ownership records."""
    for child in root.iterdir():
        stage, _ = _stage_info(child, root.parent)
        _require(all(_stage_filename(entry.name) and _plain_path(entry).is_file() for entry in stage.iterdir()),
                 'Unrecognized files in the update folder were left untouched.')
    return True


@contextmanager
def _new_update_stage(preferences):
    root = _plain_path(preferences / 'updates')
    with guard_private_tree(root, create=True, authorize_existing=_authorize_update_cache,
                            allow_locked_ancestors=True):
        cleanup_update_cache(preferences, strict=False)
        # Cleanup cannot remove the held root; a new stage inherits its private
        # ACL and receives an explicit protected ACL before any executable data.
        nonce = uuid.uuid4().hex
        stage = root / (STAGE_PREFIX + nonce)
        with guard_private_tree(stage, create=True, allow_locked_ancestors=True):
            _write_new_json(stage / 'owner.json', {'app_id': APP_ID, 'schema_version': 1, 'nonce': nonce})
            yield stage, nonce


def _stage_filename(name):
    return name in _FILES or re.fullmatch(r'(?:[a-f0-9]{64}\.payload|rollback-[0-9]{5}\.bak)', name) is not None


def _incomplete_transaction(stage):
    if (stage / 'transaction.pending').exists():
        return True
    if not (stage / 'transaction.json').exists():
        return False
    try:
        return _read_json(stage / 'transaction.json', MAX_JOURNAL_BYTES).get('state') not in ('committed', 'rolled_back')
    except (UpdateError, ValueError, OSError):
        return True


def ensure_no_interrupted_update(preferences=None):
    """Never start services against a potentially mixed application after a crash."""
    root = _plain_path(_preferences(preferences) / 'updates')
    if root.is_dir():
        for candidate in root.iterdir():
            if _STAGE.fullmatch(candidate.name):
                stage, _ = _stage_info(candidate, preferences)
                _require(not _incomplete_transaction(stage),
                         'A previous file update was interrupted. Recovery backups have been kept. '
                         'Download and run the full installer from ' + BASE_URL + '/ before opening the translator.')


def finish_installer_repair(preferences=None):
    """Called by Inno only after its complete installed-payload verification.

    A crash keeps backups until an independently verified full installation
    succeeds. This maintenance mode never starts a window, model or service.
    """
    executable = _installed_executable()
    preferences = _preferences(preferences)
    root = _plain_path(preferences / 'updates')
    repaired, preserved_temporary = 0, []
    if root.is_dir():
        for candidate in root.iterdir():
            if not _STAGE.fullmatch(candidate.name):
                continue
            stage, owner = _stage_info(candidate, preferences)
            if not _incomplete_transaction(stage):
                continue
            job = _read_json(stage / 'job.json')
            release = _signed_release(_read_bytes(stage / 'release.json'), _read_bytes(stage / 'release.sig', SIGNATURE_BYTES))
            _require(job.get('executable') == str(executable) and job.get('nonce') == owner['nonce']
                     and job.get('version') == release.version and _version(release.version) <= _version(VERSION),
                     'An interrupted update belongs to another installation. Recovery files were kept.')
            manifest_id = file_update.validate_descriptor(release.incremental, release.version)['manifest']
            data = _read_bytes(stage / 'files.json', file_update.MAX_MANIFEST_BYTES)
            _require(len(data) == manifest_id['bytes'] and hashlib.sha256(data).hexdigest() == manifest_id['sha256'],
                     'Interrupted update metadata changed. Recovery files were kept.')
            manifest = file_update.validate_manifest(data, release.version)
            paths = {row['path'] for row in manifest['files']} | {row['path'] for row in manifest['cleanup']['obsolete_files']}
            # The first journal can itself be interrupted before its rename.
            # Inno's independent full-payload verification also permits repair
            # of that state. Unauthenticated adjacent files remain untouched.
            journal = {}
            for name in ('transaction.json', 'transaction.pending'):
                if not (stage / name).exists():
                    continue
                try:
                    candidate_journal = _read_json(stage / name, MAX_JOURNAL_BYTES)
                    if (candidate_journal.get('app_id') == APP_ID and candidate_journal.get('nonce') == owner['nonce']
                            and candidate_journal.get('version') == release.version
                            and isinstance(candidate_journal.get('entries'), list)):
                        journal = candidate_journal
                        break
                except (UpdateError, ValueError, OSError):
                    pass
            # Adjacent incomplete copies have unambiguous journal-derived names
            # and native IDs. Unknown or raced files are never deleted.
            identified = set()
            for entry in journal.get('entries', []):
                if not isinstance(entry, dict):
                    continue
                relative = entry.get('path')
                try:
                    file_update.program_path(relative)
                except ValueError:
                    continue
                temporary = entry.get('temporary', '')
                if not temporary:
                    continue
                if relative not in paths or temporary != relative + '.l2ct-' + owner['nonce']:
                    continue
                identified.add(relative)
                path = _plain_path(executable.parent.joinpath(*temporary.split('/')))
                if path.exists():
                    expected = entry.get('temporary_id')
                    actual = ':'.join(str(part) for part in path_identity(path))
                    if expected and actual == expected and path.is_file() and path.stat().st_nlink == 1:
                        safe_remove_tree(path, expected_identity=[int(part) for part in actual.split(':')])
                    else:
                        preserved_temporary.append(str(path))
            for relative in paths - identified:
                path = _plain_path(executable.parent.joinpath(*(relative + '.l2ct-' + owner['nonce']).split('/')))
                if path.exists():
                    preserved_temporary.append(str(path))
            # Complete install verification is the authority for retiring these
            # backups, including when the repair installer is a newer release.
            pending = _plain_path(stage / 'transaction.pending')
            if pending.exists():
                pending.unlink()
            journal.update(app_id=APP_ID, schema_version=1, nonce=owner['nonce'],
                           version=release.version, state='committed')
            journal['repaired_by_full_installer'] = VERSION
            with pending.open('x', encoding='utf-8') as output:
                json.dump(journal, output)
                output.flush()
                os.fsync(output.fileno())
            pending.replace(stage / 'transaction.json')
            _remove_stage(stage, preferences)
            repaired += 1
    # A full installation refreshes Inno's own uninstall table. Its prior
    # incremental receipt is no longer needed and may describe an older version.
    for name in INSTALLED_RECEIPTS:
        path = _plain_path(preferences / name)
        if path.exists():
            _require(path.is_file() and path.stat().st_nlink == 1, 'An installed file receipt is not a regular private file.')
            path.unlink()
    if preserved_temporary:
        receipt = _plain_path(preferences / 'update-result.json')
        content = {'app_id': APP_ID, 'status': 'error', 'version': VERSION,
                   'message': ('The full installer repaired the translator. Unidentified temporary files were preserved for manual review: '
                               + '; '.join(preserved_temporary))[:MAX_RESULT_MESSAGE_CHARS]}
        with receipt.open('w', encoding='utf-8') as output:
            json.dump(content, output)
    return {'ok': True, 'version': VERSION, 'repaired_updates': repaired,
            'preserved_temporary_files': preserved_temporary}


def prepare_incremental_uninstall(result_path, preferences=None):
    """Export only authenticated program identities into Inno's private temp.

    The external cleanup runs after this app process exits, so DLLs added by
    later file updates can be removed without relying on the old Inno file log.
    """
    preferences = _preferences(preferences)
    receipts = [_plain_path(preferences / name) for name in INSTALLED_RECEIPTS]
    if not any(path.exists() for path in receipts):
        return False
    _require(result_path is not None, 'Incremental uninstall requires the installer temporary result path.')
    release = _signed_release(_read_bytes(receipts[1]), _read_bytes(receipts[2], SIGNATURE_BYTES))
    _require(release.version == VERSION, 'The installed file receipt differs from this application. Run the full installer to repair it.')
    identity = file_update.validate_descriptor(release.incremental, release.version)['manifest']
    data = _read_bytes(receipts[0], file_update.MAX_MANIFEST_BYTES)
    _require(len(data) == identity['bytes'] and hashlib.sha256(data).hexdigest() == identity['sha256'],
             'The installed file receipt checksum changed. Uninstall cleanup was not authorized.')
    manifest = file_update.validate_manifest(data, release.version)
    import tempfile
    folder = _plain_path(Path(result_path).parent)
    _require(folder.is_relative_to(_plain_path(tempfile.gettempdir()))
             and re.fullmatch(r'is-[a-z0-9]+\.tmp', folder.name, re.I),
             'Incremental uninstall requires the private Windows installer temporary folder.')
    helper = Path(__file__).resolve().parent / 'assets' / 'cleanup-program-files.ps1'
    with guard_private_tree(folder, authorize_existing=lambda _: True, allow_locked_ancestors=True):
        plan = {'schema_version': 1, 'product': PRODUCT, 'version': VERSION,
                'files': [{key: row[key] for key in ('path', 'bytes', 'sha256')} for row in manifest['files']],
                'preferences': str(preferences),
                'receipts': {path.name: {'bytes': path.stat().st_size, 'sha256': _digest(path)} for path in receipts}}
        script_target = _plain_path(folder / 'l2-incremental-uninstall.ps1')
        plan_target = _plain_path(folder / 'l2-incremental-uninstall.json')
        with script_target.open('xb') as output:
            output.write(helper.read_bytes())
        try:
            _write_new_json(plan_target, plan)
        except BaseException:
            script_target.unlink()
            raise
    return True


def _download(url, destination, record, operation, progress=None, offset=0, total=None):
    received, sha = 0, hashlib.sha256()
    with _open(url, operation) as response, destination.open('xb') as output:
        length = response.headers.get('Content-Length')
        _require(length is None or (length.isdecimal() and int(length) == record['bytes']),
                 'The update download has an unexpected size.')
        while True:
            operation.check()
            block = getattr(response, 'read1', response.read)(DOWNLOAD_CHUNK_BYTES)
            if not block:
                break
            received += len(block)
            _require(received <= record['bytes'], 'The update download exceeded its expected size.')
            output.write(block)
            sha.update(block)
            if progress:
                progress(offset + received, record['bytes'] if total is None else total)
    _require(received == record['bytes'] and sha.hexdigest() == record['sha256'],
             'The update download did not match the published checksum. It was not run.')


def _prepare_incremental(release, executable, stage, progress, cancelled):
    descriptor = file_update.validate_descriptor(release.incremental, release.version)
    identity = descriptor['manifest']
    with _network_scope(MAX_DOWNLOAD_SECONDS, cancelled) as operation:
        _download(BASE_URL + '/' + identity['file'], stage / 'files.json', identity, operation)
        manifest = file_update.validate_manifest(_read_bytes(stage / 'files.json', file_update.MAX_MANIFEST_BYTES), release.version)
        needed = file_update.changed_files(executable.parent, manifest, cancelled)
        # One verified payload is reusable when the inventory has identical files.
        unique = {row['sha256']: row for row in needed}
        total, received = sum(row['blob']['bytes'] for row in unique.values()), 0
        if progress:
            progress(0, total)
        for row in unique.values():
            operation.check()
            part = stage / 'blob.part'
            blob = row['blob']
            _download(BASE_URL + '/updates/blobs/' + blob['sha256'] + '.gz', part, blob,
                      operation, progress, received, total)
            raw = file_update.decode_blob(_read_bytes(part, file_update.MAX_FILE_BYTES), row)
            operation.check()
            with (stage / (row['sha256'] + '.payload')).open('xb') as output:
                output.write(raw)
            part.unlink()
            received += blob['bytes']
        if progress and total == 0:
            progress(1, 1)


def _verify_incremental_stage(release, stage, executable):
    descriptor = file_update.validate_descriptor(release.incremental, release.version)['manifest']
    data = _read_bytes(stage / 'files.json', file_update.MAX_MANIFEST_BYTES)
    _require(len(data) == descriptor['bytes'] and hashlib.sha256(data).hexdigest() == descriptor['sha256'],
             'The file-update manifest changed before installation.')
    manifest = file_update.validate_manifest(data, release.version)
    for row in file_update.changed_files(executable.parent, manifest):
        payload = _plain_path(stage / (row['sha256'] + '.payload'))
        _require(file_update.file_matches(payload, row), 'A required file-update payload is missing or changed. Retry the update.')


def prepare_update(release, *, progress=None, cancelled=None, force_installer=False):
    """Download and verify an update, but leave all execution to launch_update."""
    release = _validate_release(release)
    executable = _installed_executable()
    _require(not update_in_progress(), 'Another update is already in progress.')
    _cancelled(cancelled)
    preferences = _preferences()
    preferences.mkdir(parents=True, exist_ok=True)
    prepared = None
    try:
        with _new_update_stage(preferences) as (stage, nonce):
            mode = 'installer'
            prepared = PreparedUpdate(release, stage)
            if release.incremental is not None and not force_installer:
                try:
                    _prepare_incremental(release, executable, stage, progress, cancelled)
                    mode = 'incremental'
                except UpdateCancelled:
                    raise
                except (UpdateError, ValueError, OSError):
                    _cancelled(cancelled)
                    # Preparation has never touched program files. Only our own
                    # bounded download names are removed before installer fallback.
                    for child in stage.iterdir():
                        if child.name in ('files.json', 'blob.part') or re.fullmatch(r'[a-f0-9]{64}\.payload', child.name):
                            _plain_path(child).unlink()
            if mode == 'installer':
                part = stage / 'installer.part'
                with _network_scope(MAX_DOWNLOAD_SECONDS, cancelled) as operation:
                    _download(BASE_URL + '/downloads/' + release.file, part,
                              {'bytes': release.bytes, 'sha256': release.sha256}, operation, progress)
                _cancelled(cancelled)
                with part.open('rb') as source:
                    _require(source.read(2) == b'MZ', 'The download is not a Windows installer.')
                part.replace(stage / 'installer.exe')
            prepared = PreparedUpdate(release, stage, mode)
            (stage / 'release.json').write_bytes(release.metadata)
            (stage / 'release.sig').write_bytes(release.signature)
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
                'mode': mode,
            })
            _cancelled(cancelled)
            return prepared
    except BaseException:
        if prepared is not None:
            try:
                discard_update(prepared)
            except (UpdateError, OSError):
                pass  # Unknown files are not deleted to hide an error.
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
    # Hold path components and script bytes across the hash-to-start boundary.
    # The ready handshake is written by the parsed script itself, so the lock
    # is released only after the child has loaded that exact verified script.
    try:
        root = _plain_path(_preferences() / 'updates')
        with guard_private_tree(root, authorize_existing=_authorize_update_cache, allow_locked_ancestors=True):
            stage, owner = _stage_info(prepared.stage)
            with guard_private_tree(stage, allow_locked_ancestors=True), _locked_helper(stage / 'apply-update.ps1') as runner:
                return _launch_update_locked(prepared, stage, owner, runner)
    except BaseException:
        try:
            discard_update(prepared)
        except (UpdateError, OSError):
            pass
        raise


def _launch_update_locked(prepared, stage, owner, runner):
    release = _validate_release(prepared.release)
    staged_release = _signed_release(_read_bytes(stage / 'release.json'),
                                     _read_bytes(stage / 'release.sig', SIGNATURE_BYTES))
    _require(staged_release == release, 'The signed update details changed. The installer was not run.')
    job = _read_json(stage / 'job.json')
    executable = _installed_executable()
    _require(job.get('app_id') == APP_ID and job.get('schema_version') == 1
             and job.get('nonce') == owner['nonce'] and job.get('parent_pid') == os.getpid()
             and job.get('from_version') == VERSION and job.get('version') == release.version
             and job.get('installer_file') == release.file and job.get('installer_bytes') == release.bytes
             and job.get('installer_sha256') == release.sha256
             and job.get('executable') == str(executable) and job.get('preferences') == str(_preferences())
             and prepared.mode in ('installer', 'incremental') and job.get('mode', 'installer') == prepared.mode,
             'The update handoff record changed. The installer was not started.')
    if prepared.mode == 'incremental':
        _verify_incremental_stage(release, stage, executable)
    else:
        _require((stage / 'installer.exe').stat().st_size == release.bytes
                 and _digest(stage / 'installer.exe') == release.sha256,
                 'The downloaded installer changed. It was not run.')
    runner_hash = _digest(RUNNER_PATH)
    _require(job.get('runner_sha256') == runner_hash and hashlib.file_digest(runner, 'sha256').hexdigest() == runner_hash,
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
        (stage / 'cancel').touch(exist_ok=True)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=CHILD_STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=CHILD_STOP_TIMEOUT_SECONDS)
        # The public wrapper releases path/file locks before cleanup.
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
