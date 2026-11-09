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

ROOT = Path('/srv/l2-chat-translator')
BASE_URL = 'https://40.160.140.227'
STATIC_FILES = {'index.html', '_headers', 'robots.txt', 'assets/L2CT.png',
                'assets/L2CT.ico', 'downloads/release.json', 'downloads/release.sig',
                'downloads/SHA256SUMS.txt'}
SIGNATURE_BYTES = 384  # RSA-3072; clients verify with their independently pinned public key.
MAX_METADATA_BYTES = 64 * 1024
INSTALLER = re.compile(r'downloads/Lineage-2-Chat-Translator-[0-9]+\.[0-9]+\.[0-9]+-Setup\.exe')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


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
    require(all(name in STATIC_FILES or INSTALLER.fullmatch(name) for name in names),
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
