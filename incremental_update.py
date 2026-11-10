"""Bounded, authenticated file-update format shared by the app and publisher.

The signed release authenticates the manifest's exact bytes. Each manifest row
authenticates both its compressed transfer and its complete installed contents.
Selection always examines the full target inventory, so skipped releases and
missing or damaged files use the same protocol.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import zlib

from installation_cleanup import safe_relative_path, validate_plan

PRODUCT = 'Lineage 2 Chat Translator'
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_FILES = 4096
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
HASH_CHUNK_BYTES = 1024 * 1024
_HASH = re.compile(r'[a-f0-9]{64}\Z')
_VERSION = re.compile(r'(?:0|[1-9][0-9]{0,4})\.(?:0|[1-9][0-9]{0,4})\.(?:0|[1-9][0-9]{0,4})\Z')
_ROOT_FILES = frozenset({'Lineage2ChatTranslator.exe', 'USER-README.txt', 'THIRD-PARTY-NOTICES.txt'})
_PRIVATE = frozenset({'models', 'logs', 'captures', 'settings.json', 'runtime.json',
                      'network.log', '.git', '.build', '.build-tools', 'release', 'review_tools'})


class IncrementalError(ValueError):
    """A file update could not be safely prepared or authenticated."""


def _require(condition, message):
    if not condition:
        raise IncrementalError(message)


def _hash(value):
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _version(value):
    _require(isinstance(value, str) and _VERSION.fullmatch(value), 'Invalid file-update version.')


def validate_descriptor(descriptor, version):
    """Only the fixed versioned manifest route can be selected by a release."""
    _version(version)
    _require(isinstance(descriptor, dict) and set(descriptor) == {'schema_version', 'manifest'}
             and type(descriptor['schema_version']) is int and descriptor['schema_version'] == 1,
             'Invalid file-update descriptor.')
    row = descriptor['manifest']
    _require(isinstance(row, dict) and set(row) == {'file', 'bytes', 'sha256'}
             and row['file'] == f'updates/{version}/files.json'
             and type(row['bytes']) is int and 0 < row['bytes'] <= MAX_MANIFEST_BYTES
             and _hash(row['sha256']), 'Invalid file-update manifest identity.')
    return {'schema_version': 1, 'manifest': dict(row)}


def program_path(value):
    """Restrict writes to package files and reject ambiguous Windows names."""
    try:
        path = safe_relative_path(value)
    except ValueError as exc:
        raise IncrementalError(str(exc)) from exc
    parts = path.split('/')
    _require(path in _ROOT_FILES or (len(parts) > 1 and parts[0] in {'_internal', 'licenses'}),
             'File updates may only replace packaged program files.')
    _require(not any(part.casefold() in _PRIVATE or part.casefold().startswith('unins')
                     or part.casefold().endswith(('.local.json', '.log', '.bak', '.old'))
                     for part in parts), 'User data and uninstaller files cannot be file-update targets.')
    return path


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, 'The file-update manifest contains duplicate fields.')
        result[key] = value
    return result


def validate_manifest(data, version):
    """Validate a whole inventory; callers must authenticate its bytes first."""
    _version(version)
    _require(type(data) is bytes and 0 < len(data) <= MAX_MANIFEST_BYTES,
             'The file-update manifest is missing or too large.')
    try:
        doc = json.loads(data.decode('utf-8'), object_pairs_hook=_unique)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise IncrementalError('The file-update manifest is unreadable.') from exc
    _require(isinstance(doc, dict) and set(doc) == {'schema_version', 'product', 'platform', 'version', 'files', 'cleanup'}
             and type(doc['schema_version']) is int and doc['schema_version'] == 1
             and doc['product'] == PRODUCT and doc['platform'] == 'windows-x64'
             and doc['version'] == version, 'The file-update manifest belongs to a different release.')
    files = doc['files']
    _require(isinstance(files, list) and 0 < len(files) <= MAX_FILES,
             'Invalid file-update inventory size.')
    keys, parents, contents = set(), set(), {}
    raw_total, transfer_total = 0, 0
    for row in files:
        _require(isinstance(row, dict) and set(row) == {'path', 'bytes', 'sha256', 'blob'},
                 'Invalid file-update entry.')
        path = program_path(row['path'])
        key = path.casefold()
        ancestors = {'/'.join(key.split('/')[:i]) for i in range(1, len(key.split('/')))}
        _require(key not in keys and key not in parents and not (ancestors & keys),
                 'File-update paths collide or overlap on Windows.')
        keys.add(key)
        parents.update(ancestors)
        blob = row['blob']
        _require(type(row['bytes']) is int and 0 <= row['bytes'] <= MAX_FILE_BYTES and _hash(row['sha256'])
                 and isinstance(blob, dict) and set(blob) == {'bytes', 'sha256'}
                 and type(blob['bytes']) is int and 0 < blob['bytes'] <= MAX_FILE_BYTES and _hash(blob['sha256']),
                 'Invalid file-update size or checksum.')
        identity = (row['bytes'], blob['bytes'], blob['sha256'])
        _require(row['sha256'] not in contents or contents[row['sha256']] == identity,
                 'The same file content has inconsistent update identities.')
        contents[row['sha256']] = identity
        raw_total += row['bytes']
        transfer_total += blob['bytes']
    _require(raw_total <= MAX_TOTAL_BYTES and transfer_total <= MAX_TOTAL_BYTES,
             'The file-update inventory exceeds its size limit.')
    _require('lineage2chattranslator.exe' in keys, 'The file-update application executable is missing.')
    try:
        cleanup = validate_plan(doc['cleanup'], version)
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise IncrementalError('Invalid file-update cleanup inventory.') from exc
    _require(set(cleanup['current_paths']) == {row['path'] for row in files},
             'The file-update cleanup inventory differs from the target files.')
    doc['cleanup'] = cleanup
    return doc


def decode_blob(data, record):
    """Accept one gzip member, bounded by its authenticated expanded length."""
    blob = record['blob']
    _require(type(data) is bytes and len(data) == blob['bytes']
             and len(data) <= MAX_FILE_BYTES and hashlib.sha256(data).hexdigest() == blob['sha256'],
             'The compressed file download did not match its published checksum.')
    try:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        raw = decoder.decompress(data, record['bytes'] + 1)
        _require(decoder.eof and not decoder.unconsumed_tail and not decoder.unused_data
                 and len(raw) == record['bytes'], 'The expanded file has an unexpected size or gzip format.')
    except zlib.error as exc:
        raise IncrementalError('The compressed file download is damaged.') from exc
    _require(hashlib.sha256(raw).hexdigest() == record['sha256'],
             'The expanded file did not match its published checksum.')
    return raw


def plain_path(value):
    """Do not traverse links or Windows junctions at any level."""
    path = Path(os.path.abspath(value))
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        _require(not stat.S_ISLNK(info.st_mode) and not (getattr(info, 'st_file_attributes', 0) & 0x400),
                 'File updates cannot use linked or redirected paths.')
    return path


def _cancelled(event):
    _require(event is None or not event.is_set(), 'Update canceled. The installed translator was not changed.')


def file_matches(path, record, cancelled=None):
    path = plain_path(path)
    _cancelled(cancelled)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return False
    _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1,
             'A program file is a directory, special file or hard link. Use the full installer to repair it.')
    if before.st_size != record['bytes']:
        return False
    sha = hashlib.sha256()
    with path.open('rb') as stream:
        while True:
            _cancelled(cancelled)
            block = stream.read(HASH_CHUNK_BYTES)
            if not block:
                break
            sha.update(block)
    after = path.lstat()
    _require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
             == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
             'A program file changed during update verification. Try again.')
    return sha.hexdigest() == record['sha256']


def changed_files(root, manifest, cancelled=None):
    """Hash every current file; release numbers alone never imply intact bytes."""
    root = plain_path(root)
    _require(root.is_dir(), 'The installed application folder is missing.')
    changed = []
    for row in manifest['files']:
        path = plain_path(root.joinpath(*program_path(row['path']).split('/')))
        _require(path.is_relative_to(root), 'A file-update target escaped the installation.')
        if not file_matches(path, row, cancelled):
            changed.append(row)
    return changed
