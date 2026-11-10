"""Run on the main server to verify and atomically publish a static release.

Does not install packages, change web configuration, or control any service.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid
import zipfile
import zlib

ROOT = Path('/srv/l2-chat-translator')
BASE_URL = 'https://40.160.140.227'
STATIC_FILES = {'index.html', '_headers', 'robots.txt', 'assets/L2CT.png',
                'assets/L2CT.ico', 'downloads/release.json', 'downloads/release.sig',
                'downloads/SHA256SUMS.txt'}
SIGNATURE_BYTES = 384  # RSA-3072; clients verify with their independently pinned public key.
MAX_METADATA_BYTES = 64 * 1024
INSTALLER = re.compile(r'downloads/Lineage-2-Chat-Translator-[0-9]+\.[0-9]+\.[0-9]+-Setup\.exe')
UPDATE_MANIFEST = re.compile(r'updates/([0-9]+\.[0-9]+\.[0-9]+)/files\.json')
UPDATE_BLOB = re.compile(r'updates/blobs/[a-f0-9]{64}\.gz')
MAX_UPDATE_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_UPDATE_FILE_BYTES = 128 * 1024 * 1024
MAX_UPDATE_TOTAL_BYTES = 512 * 1024 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_updates(bundle, public, names):
    """Independently audit every compressed/raw object before public activation.

    The publisher verifies the pinned signature before export; this standalone
    server helper receives no signing key or application modules.
    """
    update_names = {name for name in names if name.startswith('updates/')}
    descriptor = public.get('incremental')
    if descriptor is None:
        require(not update_names, 'Update artifacts need signed current metadata')
        return
    require(isinstance(descriptor, dict) and set(descriptor) == {'schema_version', 'manifest'}
            and type(descriptor['schema_version']) is int and descriptor['schema_version'] == 1,
            'Invalid update descriptor')
    identity = descriptor['manifest']
    require(isinstance(identity, dict) and set(identity) == {'file', 'bytes', 'sha256'}
            and identity['file'] == 'updates/' + public['version'] + '/files.json'
            and type(identity['bytes']) is int and 0 < identity['bytes'] <= MAX_UPDATE_MANIFEST_BYTES
            and re.fullmatch('[a-f0-9]{64}', identity['sha256']), 'Invalid update manifest identity')
    require(identity['file'] in update_names, 'Missing current update manifest')
    used, verified = set(), set()
    for name in sorted(update_names):
        match = UPDATE_MANIFEST.fullmatch(name)
        if not match:
            continue
        require(bundle.getinfo(name).file_size <= MAX_UPDATE_MANIFEST_BYTES, 'Oversized update manifest')
        data = bundle.read(name)
        if name == identity['file']:
            require(len(data) == identity['bytes'] and hashlib.sha256(data).hexdigest() == identity['sha256'],
                    'Current update manifest differs from signed metadata')
        document = json.loads(data)
        require(isinstance(document, dict) and set(document) == {'schema_version', 'product', 'platform', 'version', 'files', 'cleanup'}
                and type(document['schema_version']) is int and document['schema_version'] == 1
                and document['product'] == 'Lineage 2 Chat Translator' and document['platform'] == 'windows-x64'
                and document['version'] == match[1], 'Invalid update manifest product/version')
        rows = document['files']
        require(isinstance(rows, list) and 0 < len(rows) <= 4096, 'Invalid update file count')
        used.add(name)
        paths, raw_total, wire_total = set(), 0, 0
        for row in rows:
            require(isinstance(row, dict) and set(row) == {'path', 'bytes', 'sha256', 'blob'}, 'Invalid update file entry')
            path, blob = row['path'], row['blob']
            require(isinstance(path, str) and (path in {'Lineage2ChatTranslator.exe', 'USER-README.txt', 'THIRD-PARTY-NOTICES.txt'}
                    or path.startswith(('_internal/', 'licenses/')))
                    and not any(part in {'', '.', '..'} for part in path.split('/'))
                    and not re.search(r'[\\:\x00-\x1f*?"<>|]', path) and path.casefold() not in paths,
                    'Invalid or duplicate update program path')
            paths.add(path.casefold())
            require(type(row['bytes']) is int and 0 <= row['bytes'] <= MAX_UPDATE_FILE_BYTES
                    and isinstance(row['sha256'], str) and re.fullmatch('[a-f0-9]{64}', row['sha256'])
                    and isinstance(blob, dict) and set(blob) == {'bytes', 'sha256'}
                    and type(blob['bytes']) is int and 0 < blob['bytes'] <= MAX_UPDATE_FILE_BYTES
                    and isinstance(blob['sha256'], str) and re.fullmatch('[a-f0-9]{64}', blob['sha256']),
                    'Invalid update file size/checksum')
            raw_total += row['bytes']
            wire_total += blob['bytes']
            require(raw_total <= MAX_UPDATE_TOTAL_BYTES and wire_total <= MAX_UPDATE_TOTAL_BYTES,
                    'Update inventory exceeds size limits')
            target = 'updates/blobs/' + blob['sha256'] + '.gz'
            used.add(target)
            require(target in update_names and bundle.getinfo(target).file_size == blob['bytes'], 'Missing or incorrect update object')
            check = (target, row['bytes'], row['sha256'])
            if check in verified:
                continue
            compressed = bundle.read(target)
            require(hashlib.sha256(compressed).hexdigest() == blob['sha256'], 'Compressed update hash mismatch')
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            try:
                raw = decoder.decompress(compressed, row['bytes'] + 1)
            except zlib.error as exc:
                raise ValueError('Invalid compressed update object') from exc
            require(decoder.eof and not decoder.unconsumed_tail and not decoder.unused_data
                    and len(raw) == row['bytes'] and hashlib.sha256(raw).hexdigest() == row['sha256'],
                    'Expanded update hash/size mismatch')
            verified.add(check)
        require('lineage2chattranslator.exe' in paths and isinstance(document['cleanup'], dict)
                and document['cleanup'].get('version') == document['version']
                and set(document['cleanup'].get('current_paths', [])) == {row['path'] for row in rows},
                'Update inventory or cleanup contract mismatch')
    require(used == update_names, 'Unreferenced or missing public update artifacts')


def verify_bundle(archive, manifest):
    require(manifest['schema_version'] == 1 and manifest['base_url'] == BASE_URL,
            'Wrong export schema or destination')
    version = manifest['version']
    require(re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version), 'Invalid version')
    identity = {key: manifest[key] for key in ('schema_version', 'version', 'base_url', 'installer', 'files')}
    encoded = json.dumps(identity, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    require(manifest['release_id'] == version + '-' + hashlib.sha256(encoded).hexdigest()[:12],
            'Invalid release identity')
    require(not archive.is_symlink() and archive.is_file(), 'Archive must be a regular file')
    require(archive.name == manifest['archive']['file']
            and archive.stat().st_size == manifest['archive']['bytes']
            and digest(archive) == manifest['archive']['sha256'], 'Archive verification failed')
    records = manifest['files']
    names = [row['path'] for row in records]
    require(len(names) == len(set(names)) and STATIC_FILES.issubset(names), 'Missing or duplicate public files')
    require(all(name in STATIC_FILES or INSTALLER.fullmatch(name) or UPDATE_MANIFEST.fullmatch(name)
                or UPDATE_BLOB.fullmatch(name) for name in names),
            'Non-public file in release')
    installer = manifest['installer']
    require(installer['file'] == f'Lineage-2-Chat-Translator-{version}-Setup.exe', 'Wrong current installer')
    require({'path': 'downloads/' + installer['file'], 'bytes': installer['bytes'],
             'sha256': installer['sha256']} in records, 'Installer metadata mismatch')
    with zipfile.ZipFile(archive) as bundle:
        require(bundle.namelist() == names, 'ZIP does not match manifest')
        for row in records:
            member = bundle.getinfo(row['path'])
            require(stat.S_ISREG(member.external_attr >> 16), 'Non-regular ZIP member')
            with bundle.open(member) as stream:
                sha = hashlib.file_digest(stream, 'sha256').hexdigest()
            require(member.file_size == row['bytes'] and sha == row['sha256'],
                    'Incorrect ZIP content: ' + row['path'])
        require(bundle.getinfo('downloads/release.json').file_size <= MAX_METADATA_BYTES
                and bundle.getinfo('downloads/release.sig').file_size == SIGNATURE_BYTES,
                'Invalid signed release metadata size')
        # The public bundle carries the detached signature unchanged. Its publisher
        # key never belongs on this host; independent verification happens on clients.
        public = json.loads(bundle.read('downloads/release.json'))
        require(public.get('version') == version and public.get('installer') == installer,
                'Public release metadata differs from manifest')
        verify_updates(bundle, public, names)
        checksums = {}
        for line in bundle.read('downloads/SHA256SUMS.txt').decode('utf-8').splitlines():
            match = re.fullmatch(r'([0-9a-f]{64})  (Lineage-2-Chat-Translator-[0-9]+\.[0-9]+\.[0-9]+-Setup\.exe)', line)
            require(match is not None and match[2] not in checksums, 'Invalid public checksums')
            checksums[match[2]] = match[1]
        require(checksums == {row['path'].split('/')[-1]: row['sha256'] for row in records
                              if INSTALLER.fullmatch(row['path'])}, 'Public checksums differ from manifest')
    return records


def verify_tree(directory, records):
    entries = list(directory.rglob('*'))
    require(all(not item.is_symlink() for item in entries), 'Links within public release')
    require({item.relative_to(directory).as_posix() for item in entries if item.is_file()}
            == {row['path'] for row in records}, 'Unexpected files in public release')
    for row in records:
        path = directory / row['path']
        require(path.stat().st_size == row['bytes'] and digest(path) == row['sha256'],
                'Installed file verification failed: ' + row['path'])


def publish(archive, manifest, root=ROOT):
    records = verify_bundle(archive, manifest)
    root = Path(root)
    require(not root.is_symlink(), 'Linked deployment root')
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    releases, manifests, current = root / 'releases', root / 'manifests', root / 'current'
    for directory in (releases, manifests):
        require(not directory.is_symlink(), 'Linked deployment directory')
        directory.mkdir(exist_ok=True)
    previous = None
    if current.is_symlink():
        previous = current.resolve(strict=True)
        require(previous.parent == releases and previous.is_dir(), 'Unexpected current release target')
    else:
        require(not current.exists(), 'Current deployment is not a release symlink')
    if previous:
        by_name = {row['path']: row for row in records}
        for old in (previous / 'downloads').glob('*.exe'):
            name = 'downloads/' + old.name
            require(not old.is_symlink() and name in by_name and digest(old) == by_name[name]['sha256'],
                    'Existing installer would change or disappear: ' + old.name)
        for old in (previous / 'updates').rglob('*'):
            require(not old.is_symlink(), 'Linked previous update artifact')
            if not old.is_file():
                continue
            name = old.relative_to(previous).as_posix()
            require(not old.is_symlink() and name in by_name and digest(old) == by_name[name]['sha256'],
                    'Existing update artifact would change or disappear: ' + name)
    release = releases / manifest['release_id']
    require(not release.is_symlink(), 'Linked release destination')
    if release.exists():
        verify_tree(release, records)
    else:
        with tempfile.TemporaryDirectory(prefix='.staging-', dir=releases) as temporary:
            stage = Path(temporary) / 'public'
            stage.mkdir(mode=0o755)
            with zipfile.ZipFile(archive) as bundle:
                for row in records:
                    path = stage / row['path']
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(row['path']) as source, path.open('xb') as target:
                        while block := source.read(1024 * 1024):
                            target.write(block)
                    path.chmod(0o644)
            for directory in (item for item in stage.rglob('*') if item.is_dir()):
                directory.chmod(0o755)
            verify_tree(stage, records)
            stage.rename(release)
    manifest_path = manifests / (manifest['release_id'] + '.json')
    require(not manifest_path.is_symlink(), 'Linked manifest destination')
    serialized = json.dumps(manifest, indent=2) + '\n'
    if manifest_path.exists():
        require(json.loads(manifest_path.read_text()) == manifest, 'Existing manifest differs')
    else:
        with manifest_path.open('x') as stream:
            stream.write(serialized)
        manifest_path.chmod(0o644)
    pending = root / ('.current-' + uuid.uuid4().hex)
    pending.symlink_to(release)
    try:
        os.replace(pending, current)
    finally:
        if pending.is_symlink():
            pending.unlink()
    return {'release_id': manifest['release_id'], 'version': manifest['version'],
            'previous_release': previous.name if previous else None,
            'current': str(current.resolve()), 'installer_sha256': manifest['installer']['sha256']}


def publish_and_announce(archive, manifest, root=ROOT, announcer=None):
    """A Discord outage must never undo or misreport a successful publication."""
    result = publish(archive, manifest, root=root)
    try:
        if announcer is None:
            from discord_release import announce_release
            announcer = announce_release
        result['discord'] = announcer(manifest, root=root)
    except Exception:
        # Do not print external errors: they can include credentials or bodies.
        result['discord'] = {'status': 'error',
                             'message': 'Release is live; Discord announcement needs attention.'}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    args = parser.parse_args()
    require(os.geteuid() == 0, 'Run with sudo so public files remain root-owned')
    require(not args.manifest.is_symlink() and args.manifest.is_file(), 'Invalid manifest file')
    manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
    print(json.dumps(publish_and_announce(args.archive, manifest), indent=2))


if __name__ == '__main__':
    main()
