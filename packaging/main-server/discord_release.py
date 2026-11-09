"""Post verified public releases through a channel-scoped Discord bot.

This is server-side release tooling, never shipped in the translator. It uses
one-shot HTTPS requests, not a gateway connection or a background service.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import ssl
import stat
import tempfile
import time
from urllib import error, request


ROOT = Path('/srv/l2-chat-translator')
CONFIG_PATH = Path('/etc/l2ct-discord/config.json')
STATE_DIR = Path('/var/lib/l2ct-discord')
BASE_URL = 'https://40.160.140.227'
API_ORIGIN = 'https://discord.com/api/v10'
AUTHORIZED_GUILD_ID = '1569092047321829577'
ANNOUNCEMENT_CHANNEL_NAME = 'updates-and-announcements'
REQUEST_TIMEOUT = 15
DOWNLOAD_DEADLINE = 60
MAX_JSON_BYTES = 1024 * 1024
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_INSTALLER_BYTES = 256 * 1024 * 1024
STREAM_BYTES = 64 * 1024
MAX_MESSAGE_UNITS = 2000  # Conservative UTF-16 bound for Discord message content.
VERSION_RE = re.compile(r'[0-9]+\.[0-9]+\.[0-9]+')
SNOWFLAKE_RE = re.compile(r'[0-9]{17,20}')
SHA_RE = re.compile(r'[0-9a-f]{64}')
TOKEN_RE = re.compile(r'[A-Za-z0-9._-]{30,256}')


class AnnouncementError(ValueError):
    """Messages are developer-written and contain no remote body or secret."""


class DiscordAPIError(AnnouncementError):
    def __init__(self, status=None, *, ambiguous=False):
        self.status = status
        self.ambiguous = ambiguous
        message = f'Discord rejected the request (HTTP {status}).' if status else 'Discord could not confirm the request.'
        super().__init__(message)


def require(condition, message):
    if not condition:
        raise AnnouncementError(message)


def snowflake(value):
    return isinstance(value, str) and bool(SNOWFLAKE_RE.fullmatch(value)) and 0 < int(value) < 2**64


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def https_opener():
    # The bot credential must never follow a redirect or an environment proxy.
    return request.build_opener(request.ProxyHandler({}),
                                request.HTTPSHandler(context=ssl.create_default_context()), NoRedirect())


class DiscordClient:
    def __init__(self, token):
        require(isinstance(token, str) and TOKEN_RE.fullmatch(token), 'Invalid bot token format.')
        self._token = token
        self._opener = https_opener()

    def get(self, path):
        return self._request('GET', path)

    def post_message(self, channel_id, payload):
        require(snowflake(channel_id), 'Invalid announcement channel ID.')
        return self._request('POST', f'/channels/{channel_id}/messages', payload)

    def edit_message(self, channel_id, message_id, payload):
        require(snowflake(channel_id) and snowflake(message_id), 'Invalid announcement message ID.')
        # Explicitly clear the old embed; edits must also suppress mentions.
        body = {key: payload[key] for key in ('content', 'embeds', 'allowed_mentions')}
        return self._request('PATCH', f'/channels/{channel_id}/messages/{message_id}', body)

    def _request(self, method, path, payload=None):
        allowed_get = path == '/oauth2/applications/@me' or re.fullmatch(
            r'/(?:channels/[0-9]{17,20}|guilds/[0-9]{17,20}/channels)', path)
        allowed_post = re.fullmatch(r'/channels/[0-9]{17,20}/messages', path)
        allowed_edit = re.fullmatch(r'/channels/[0-9]{17,20}/messages/[0-9]{17,20}', path)
        require((method == 'GET' and allowed_get) or (method == 'POST' and allowed_post)
                or (method == 'PATCH' and allowed_edit),
                'Unsupported Discord API operation.')
        mutating = method in ('POST', 'PATCH')
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
        headers = {'Authorization': 'Bot ' + self._token,
                   'User-Agent': 'L2ChatTranslatorReleaseBot/1.0', 'Accept': 'application/json'}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        req = request.Request(API_ORIGIN + path, data=body, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=REQUEST_TIMEOUT) as response:
                if response.status != 200 or response.geturl() != API_ORIGIN + path:
                    raise DiscordAPIError(response.status, ambiguous=mutating)
                raw = response.read(MAX_JSON_BYTES + 1)
                if len(raw) > MAX_JSON_BYTES:
                    raise DiscordAPIError(ambiguous=mutating)
                decoded = json.loads(raw)
                if not isinstance(decoded, (dict, list)):
                    raise DiscordAPIError(ambiguous=mutating)
                return decoded
        except error.HTTPError as exc:
            # Do not log the response, URL, request headers or chained exception.
            status = exc.code
            exc.close()
            raise DiscordAPIError(status, ambiguous=mutating and not 400 <= status < 500) from None
        except DiscordAPIError:
            raise
        except Exception:
            # Also sanitize partial-read/TLS/parser failures whose exceptions
            # can contain request or response fragments.
            raise DiscordAPIError(ambiguous=mutating) from None


def _inside(path, directory):
    return path == directory or directory in path.parents


def _private_directory(path, *, create=False):
    path = Path(path).absolute()
    require(path.resolve() == path, 'Private storage must not use symbolic links.')
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(path.is_dir() and not path.is_symlink(), 'Private storage directory is missing.')
    info = path.stat()
    if os.name == 'posix':
        require(info.st_uid == os.geteuid() and not info.st_mode & 0o077,
                'Private storage must be owned by this user with mode 0700.')
    return path


def _private_file(path):
    require(path.is_file() and not path.is_symlink(), 'Private file must be a regular file.')
    info = path.stat()
    require(stat.S_ISREG(info.st_mode), 'Private file must be a regular file.')
    if os.name == 'posix':
        require(info.st_uid == os.geteuid() and not info.st_mode & 0o077,
                'Private file must be owned by this user with mode 0600.')


def _read_json(path, limit=MAX_JSON_BYTES):
    require(path.stat().st_size <= limit, 'JSON file exceeds its size limit.')
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    require(len(data) <= limit, 'JSON file exceeds its size limit.')
    try:
        return json.loads(data)
    except (ValueError, UnicodeError):
        raise AnnouncementError('JSON file is invalid.') from None


def _config(path, root):
    path = Path(path).absolute()
    require(not _inside(path.resolve(), root.resolve()), 'Bot credentials must be outside the website tree.')
    _private_directory(path.parent)
    _private_file(path)
    config = _read_json(path, 8192)
    require(isinstance(config, dict) and set(config) <= {
        'bot_token', 'application_id', 'guild_id', 'channel_id', 'channel_name'}, 'Invalid bot configuration.')
    require(all(snowflake(config.get(key)) for key in ('application_id', 'guild_id', 'channel_id')),
            'Bot application, server and channel IDs are required.')
    require(isinstance(config.get('bot_token'), str) and TOKEN_RE.fullmatch(config['bot_token']),
            'Invalid bot token format.')
    require(config['guild_id'] == AUTHORIZED_GUILD_ID
            and config.get('channel_name') == ANNOUNCEMENT_CHANNEL_NAME,
            'Bot configuration must target the authorized project server and announcement channel.')
    return config


@contextmanager
def _state_lock(directory):
    lock = directory / 'announce.lock'
    if lock.exists():
        _private_file(lock)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(lock, flags, 0o600)
    locked = False
    try:
        if os.name == 'posix':
            import fcntl
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AnnouncementError('Another announcement is already in progress.') from None
        elif os.name == 'nt':
            import msvcrt
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b'\0')
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError:
                raise AnnouncementError('Another announcement is already in progress.') from None
        else:
            raise AnnouncementError('File locking is unsupported on this platform.')
        locked = True
        yield
    finally:
        if locked:
            if os.name == 'posix':
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            else:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        os.close(descriptor)


def _load_state(directory):
    path = directory / 'announcements.json'
    if not path.exists():
        require(not path.is_symlink(), 'Invalid announcement state.')
        return {'schema_version': 1, 'releases': {}}
    _private_file(path)
    state = _read_json(path)
    require(isinstance(state, dict) and state.get('schema_version') == 1
            and isinstance(state.get('releases'), dict), 'Invalid announcement state.')
    for key, record in state['releases'].items():
        require(isinstance(key, str) and isinstance(record, dict)
                and record.get('status') in ('pending', 'sent')
                and isinstance(record.get('installer_sha256'), str)
                and SHA_RE.fullmatch(record['installer_sha256']), 'Invalid announcement state.')
        if record['status'] == 'sent':
            require(snowflake(record.get('message_id')), 'Invalid announcement state.')
    return state


def _save_state(directory, state):
    """Replace one private ledger atomically while holding the serialization lock."""
    target = directory / 'announcements.json'
    if target.exists() or target.is_symlink():
        _private_file(target)
    data = (json.dumps(state, indent=2) + '\n').encode('utf-8')
    require(len(data) <= MAX_JSON_BYTES, 'Announcement ledger exceeds its size limit.')
    descriptor, temporary = tempfile.mkstemp(prefix='.announce-', dir=directory)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        if os.name == 'posix':
            # The pending record must survive a power loss before Discord sees
            # the request, including the directory entry created by replace().
            directory_fd = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _validate_manifest(manifest):
    require(isinstance(manifest, dict) and manifest.get('schema_version') == 1
            and manifest.get('base_url') == BASE_URL, 'Invalid announcement release manifest.')
    version = manifest.get('version')
    require(isinstance(version, str) and VERSION_RE.fullmatch(version) and len(version) <= 32,
            'Invalid release version.')
    installer = manifest.get('installer')
    require(isinstance(installer, dict) and installer.get('file') == f'Lineage-2-Chat-Translator-{version}-Setup.exe'
            and type(installer.get('bytes')) is int and 0 < installer['bytes'] <= MAX_INSTALLER_BYTES
            and isinstance(installer.get('sha256'), str) and SHA_RE.fullmatch(installer['sha256']),
            'Invalid installer metadata.')
    rows = manifest.get('files')
    require(isinstance(rows, list) and 0 < len(rows) <= 1000, 'Invalid public file manifest.')
    records = {}
    for row in rows:
        require(isinstance(row, dict) and isinstance(row.get('path'), str)
                and row['path'] not in records and type(row.get('bytes')) is int and row['bytes'] >= 0
                and isinstance(row.get('sha256'), str) and SHA_RE.fullmatch(row['sha256']),
                'Invalid public file manifest.')
        records[row['path']] = row
    require('index.html' in records and 'downloads/release.json' in records
            and records.get('downloads/' + installer['file']) == {
                'path': 'downloads/' + installer['file'], 'bytes': installer['bytes'], 'sha256': installer['sha256']},
            'Release manifest is missing public files.')
    identity = {key: manifest[key] for key in ('schema_version', 'version', 'base_url', 'installer', 'files')}
    hashed = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')).hexdigest()[:12]
    require(manifest.get('release_id') == version + '-' + hashed, 'Release identity mismatch.')
    return records


def _assert_current(root, release_id):
    root = Path(root).absolute()
    require(root.resolve() == root and root.is_dir(), 'Invalid website root.')
    releases, current = root / 'releases', root / 'current'
    require(not releases.is_symlink() and current.is_symlink(), 'Website has no published release.')
    expected = releases / release_id
    require(expected.is_dir() and not expected.is_symlink() and current.resolve(strict=True) == expected,
            'The requested release is not the current published release.')


def _download_verified(opener, path, record, limit, *, keep):
    require(0 < record['bytes'] <= limit, 'Public release file exceeds its size limit.')
    url = BASE_URL + path
    req = request.Request(url, headers={'User-Agent': 'L2ChatTranslatorReleaseVerifier/1.0', 'Cache-Control': 'no-cache'})
    deadline = time.monotonic() + DOWNLOAD_DEADLINE
    count, digest, parts = 0, hashlib.sha256(), []
    try:
        with opener.open(req, timeout=REQUEST_TIMEOUT) as response:
            require(response.status == 200 and response.geturl() == url, 'Public release URL redirected or failed.')
            while True:
                require(time.monotonic() <= deadline, 'Public release verification timed out.')
                chunk = response.read(min(STREAM_BYTES, record['bytes'] - count + 1))
                if not chunk:
                    break
                count += len(chunk)
                require(count <= record['bytes'], 'Public release file size differs from its manifest.')
                digest.update(chunk)
                if keep:
                    parts.append(chunk)
    except (OSError, error.URLError):
        raise AnnouncementError('Could not verify the release over public HTTPS.') from None
    require(count == record['bytes'] and digest.hexdigest() == record['sha256'],
            'Public release file differs from its verified manifest.')
    return b''.join(parts) if keep else None


class ReleaseNotes(HTMLParser):
    def __init__(self, version):
        super().__init__(convert_charrefs=True)
        self.target = 'release-' + version
        self.in_article = False
        self.in_item = False
        self.matches = 0
        self.buffer = []
        self.notes = []

    def handle_starttag(self, tag, attrs):
        if tag == 'article' and dict(attrs).get('aria-labelledby') == self.target:
            self.in_article = True
            self.matches += 1
        if self.in_article and tag == 'li':
            self.in_item, self.buffer = True, []

    def handle_data(self, data):
        if self.in_article and self.in_item:
            self.buffer.append(data)

    def handle_endtag(self, tag):
        if tag == 'li' and self.in_item:
            self.notes.append(' '.join(''.join(self.buffer).split()))
            self.in_item = False
        elif tag == 'article':
            self.in_article = self.in_item = False


def verified_notes(manifest, root=ROOT):
    records = _validate_manifest(manifest)
    _assert_current(root, manifest['release_id'])
    opener = https_opener()
    page = _download_verified(opener, '/', records['index.html'], MAX_PAGE_BYTES, keep=True)
    metadata = _download_verified(opener, '/downloads/release.json', records['downloads/release.json'], MAX_JSON_BYTES, keep=True)
    try:
        metadata = json.loads(metadata)
        require(metadata.get('version') == manifest['version'] and metadata.get('installer') == manifest['installer']
                and metadata.get('validation', {}).get('packaged_self_test_passed') is True,
                'Public release metadata does not identify a verified build.')
        parser = ReleaseNotes(manifest['version'])
        parser.feed(page.decode('utf-8'))
    except (ValueError, UnicodeError, AttributeError):
        raise AnnouncementError('Public release metadata or changelog is invalid.') from None
    require(parser.matches == 1 and parser.notes and all(parser.notes), 'Current public changelog is missing or ambiguous.')
    installer = manifest['installer']
    _download_verified(opener, '/downloads/' + installer['file'], records['downloads/' + installer['file']],
                       MAX_INSTALLER_BYTES, keep=False)
    _assert_current(root, manifest['release_id'])
    return parser.notes


def _markdown(text):
    # Changelog is plain text, not executable Discord markup or mention syntax.
    text = re.sub(r'([\\`*_\[\]<>|~])', r'\\\1', text)
    return text.replace('@', '@\u200b')


def release_payload(manifest, notes, *, channel_id=None):
    """Return a public, bounded announcement; no configuration or credentials."""
    _validate_manifest(manifest)
    require(isinstance(notes, list) and notes and all(isinstance(note, str) and note.strip() for note in notes),
            'Release notes are required.')
    version = manifest['version']
    changes = '\n'.join('• ' + _markdown(note.strip()) for note in notes)
    content = (f'**Lineage 2 Chat Translator — v{version}**\n\n'
               f"**What changed**\n{changes}")
    # Do not silently omit fixes or send readers away to finish the write-up.
    require(len(content.encode('utf-16-le')) // 2 <= MAX_MESSAGE_UNITS,
            'Release write-up exceeds the message limit; shorten the release notes before announcing.')
    require(channel_id is None or snowflake(channel_id), 'Invalid announcement channel ID.')
    nonce_scope = 'l2ct-release:' + (channel_id or 'preview') + ':' + version
    nonce = str(int(hashlib.sha256(nonce_scope.encode('ascii')).hexdigest()[:16], 16))
    return {'content': content, 'embeds': [], 'allowed_mentions': {'parse': []},
            'nonce': nonce, 'enforce_nonce': True}


def announce_release(manifest, *, root=ROOT, config_path=CONFIG_PATH, state_dir=STATE_DIR, edit_existing=False):
    """Return a safe delivery status; notification failure never undoes release."""
    try:
        config_path, root, state_dir = Path(config_path), Path(root), Path(state_dir)
        if not config_path.exists() and not config_path.is_symlink():
            return {'status': 'unconfigured'}
        config = _config(config_path, root)
        _validate_manifest(manifest)
        require(not _inside(state_dir.resolve(), root.resolve()), 'Announcement state must be outside the website tree.')
        state_dir = _private_directory(state_dir, create=True)
        key = '/'.join((config['guild_id'], config['channel_id'], manifest['version']))
        with _state_lock(state_dir):
            state = _load_state(state_dir)
            previous = state['releases'].get(key)
            if previous:
                require(previous['installer_sha256'] == manifest['installer']['sha256'],
                        'Previously announced version has different installer bytes.')
                if previous['status'] == 'sent' and not edit_existing:
                    return {'status': 'already_posted', 'message_id': previous['message_id'], 'version': manifest['version']}
                if previous['status'] != 'sent':
                    return {'status': 'delivery_unconfirmed', 'version': manifest['version'],
                            'message': 'A previous attempt may have posted. Check Discord before retrying; automatic reposting is disabled.'}
            require(not edit_existing or previous, 'No confirmed announcement exists to edit; no new message was sent.')
            notes = verified_notes(manifest, root)
            client = DiscordClient(config['bot_token'])
            application = client.get('/oauth2/applications/@me')
            require(isinstance(application, dict) and application.get('id') == config['application_id'],
                    'Bot token belongs to a different application.')
            channel = client.get('/channels/' + config['channel_id'])
            require(isinstance(channel, dict) and channel.get('id') == config['channel_id']
                    and channel.get('guild_id') == config['guild_id'] and channel.get('type') in (0, 5),
                    'Announcement channel does not match the configured server or is not a text channel.')
            require(not config.get('channel_name') or channel.get('name') == config['channel_name'],
                    'Announcement channel was renamed; verify its configuration.')
            payload = release_payload(manifest, notes, channel_id=config['channel_id'])
            _assert_current(root, manifest['release_id'])
            if edit_existing:
                # Only edit the ledger's confirmed message; never create a replacement.
                message_id = previous['message_id']
                try:
                    edited = client.edit_message(config['channel_id'], message_id, payload)
                    if (not isinstance(edited, dict) or edited.get('id') != message_id
                            or edited.get('channel_id') != config['channel_id']
                            or edited.get('content') != payload['content'] or edited.get('embeds') != []):
                        raise DiscordAPIError(ambiguous=True)
                except DiscordAPIError as exc:
                    return {'status': 'edit_unconfirmed' if exc.ambiguous else 'not_edited',
                            'version': manifest['version'], 'message_id': message_id,
                            'message': 'The edit was not confirmed. No replacement message was sent.'}
                except Exception:
                    return {'status': 'edit_unconfirmed', 'version': manifest['version'], 'message_id': message_id,
                            'message': 'The edit was not confirmed. No replacement message was sent.'}
                return {'status': 'edited', 'version': manifest['version'], 'message_id': message_id,
                        'message_url': f'https://discord.com/channels/{config["guild_id"]}/{config["channel_id"]}/{message_id}',
                        'content': edited['content'], 'embeds': edited['embeds']}
            state['releases'][key] = {'status': 'pending', 'installer_sha256': manifest['installer']['sha256'],
                                      'nonce': payload['nonce'], 'started_at': int(time.time())}
            _save_state(state_dir, state)
            try:
                posted = client.post_message(config['channel_id'], payload)
                if not isinstance(posted, dict) or not snowflake(posted.get('id')) or posted.get('channel_id') != config['channel_id']:
                    raise DiscordAPIError(ambiguous=True)
            except DiscordAPIError as exc:
                if not exc.ambiguous:
                    del state['releases'][key]
                    _save_state(state_dir, state)
                return {'status': 'delivery_unconfirmed' if exc.ambiguous else 'not_posted',
                        'version': manifest['version'], 'http_status': exc.status,
                        'message': 'Check Discord before retrying.' if exc.ambiguous else str(exc)}
            except Exception:
                return {'status': 'delivery_unconfirmed', 'version': manifest['version'],
                        'message': 'Discord did not confirm delivery. Check Discord before retrying.'}
            state['releases'][key] = {'status': 'sent', 'message_id': posted['id'],
                                      'installer_sha256': manifest['installer']['sha256'], 'posted_at': int(time.time())}
            try:
                _save_state(state_dir, state)
            except Exception:
                return {'status': 'posted_state_unconfirmed', 'version': manifest['version'], 'message_id': posted['id'],
                        'message_url': f'https://discord.com/channels/{config["guild_id"]}/{config["channel_id"]}/{posted["id"]}',
                        'message': 'Discord accepted the message but its delivery record could not be confirmed durable. Do not repost; check the private delivery state.'}
            return {'status': 'posted', 'version': manifest['version'], 'message_id': posted['id'],
                    'message_url': f'https://discord.com/channels/{config["guild_id"]}/{config["channel_id"]}/{posted["id"]}'}
    except DiscordAPIError as exc:
        return {'status': 'not_posted', 'http_status': exc.status, 'message': str(exc)}
    except AnnouncementError as exc:
        return {'status': 'not_posted', 'message': str(exc)}
    except Exception:
        # Never expose exception repr: a transport/configuration error may embed a secret.
        return {'status': 'not_posted', 'message': 'Announcement could not complete. Check private configuration and delivery state before retrying.'}


def current_manifest(root=ROOT):
    root = Path(root).absolute()
    current = root / 'current'
    require(current.is_symlink(), 'Website has no published release.')
    release = current.resolve(strict=True)
    require(release.parent == root / 'releases' and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+-[0-9a-f]{12}', release.name),
            'Invalid current release target.')
    path = root / 'manifests' / (release.name + '.json')
    require(path.is_file() and not path.is_symlink(), 'Current release manifest is missing.')
    manifest = _read_json(path)
    _validate_manifest(manifest)
    _assert_current(root, manifest['release_id'])
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--current', action='store_true', required=True, help='Use the current published release.')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--preview', action='store_true', help='Verify public files and print the payload without posting.')
    mode.add_argument('--edit-existing', action='store_true', help='Edit the confirmed current announcement; never post a replacement.')
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--config', type=Path, default=CONFIG_PATH)
    parser.add_argument('--state-dir', type=Path, default=STATE_DIR)
    args = parser.parse_args(argv)
    try:
        manifest = current_manifest(args.root)
        if args.preview:
            result = {'status': 'preview', 'version': manifest['version'],
                      'payload': release_payload(manifest, verified_notes(manifest, args.root))}
        else:
            result = announce_release(manifest, root=args.root, config_path=args.config, state_dir=args.state_dir,
                                      edit_existing=args.edit_existing)
    except AnnouncementError as exc:
        result = {'status': 'not_posted', 'message': str(exc)}
    except Exception:
        result = {'status': 'not_posted', 'message': 'Could not load the current verified release.'}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] in ('posted', 'edited', 'already_posted', 'unconfigured', 'preview') else 2


if __name__ == '__main__':
    raise SystemExit(main())
