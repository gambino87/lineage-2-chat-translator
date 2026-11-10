"""Private release-bot checks; all Discord and public HTTPS traffic is mocked."""
from contextlib import ExitStack, redirect_stdout
import hashlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib import error


_SPEC = importlib.util.spec_from_file_location(
    'l2_discord_release', Path(__file__).resolve().parents[1] / 'packaging/main-server/discord_release.py')
bot = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bot)

APP = '1569092047321829500'
GUILD = '1569092047321829577'
CHANNEL = '1569092047321829588'
MESSAGE = '1569092047321829599'
TOKEN = 'Synthetic.Bot.Token.NotARealCredential0123456789'


def identify(manifest):
    identity = {key: manifest[key] for key in ('schema_version', 'version', 'base_url', 'installer', 'files')}
    encoded = json.dumps(identity, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    manifest['release_id'] = manifest['version'] + '-' + hashlib.sha256(encoded).hexdigest()[:12]
    return manifest


def release(version='1.2.3', text='Improved Party chat.'):
    installer_name = f'Lineage-2-Chat-Translator-{version}-Setup.exe'
    installer_bytes = b'Synthetic installer; never executed.'
    installer = {'file': installer_name, 'bytes': len(installer_bytes),
                 'sha256': hashlib.sha256(installer_bytes).hexdigest()}
    page = (f'<html><article aria-labelledby="release-{version}"><h3>v{version}</h3>'
            f'<ul><li>{text}</li><li>Saved selections &amp; reply links.</li></ul></article>'
            '<article aria-labelledby="release-0.1.0"><ul><li>Old note.</li></ul></article></html>').encode()
    files = {'index.html': page, 'downloads/release.json': json.dumps({
        'version': version, 'installer': installer, 'validation': {'packaged_self_test_passed': True}}).encode(),
        'downloads/' + installer_name: installer_bytes}
    manifest = identify({'schema_version': 1, 'base_url': bot.BASE_URL, 'version': version, 'installer': installer,
                         'files': [{'path': name, 'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
                                   for name, body in sorted(files.items())]})
    return manifest, files


class Response(io.BytesIO):
    def __init__(self, body, url, status=200):
        super().__init__(body)
        self.url, self.status = url, status

    def geturl(self):
        return self.url


class SourceVerificationTests(unittest.TestCase):
    def setUp(self):
        self.manifest, self.files = release()
        self.requests = []

    def source_opener(self, change=None):
        def open_response(req, timeout):
            self.requests.append(req)
            path = req.full_url.removeprefix(bot.BASE_URL + '/') or 'index.html'
            content = self.files[path]
            return Response(change(path, content) if change else content, req.full_url)
        return Mock(open=Mock(side_effect=open_response))

    def test_verifies_live_page_metadata_installer_and_extracts_only_current_notes(self):
        with patch.object(bot, '_assert_current') as current, patch.object(bot, 'https_opener', return_value=self.source_opener()):
            self.assertEqual(bot.verified_notes(self.manifest), ['Improved Party chat.', 'Saved selections & reply links.'])
        self.assertEqual(current.call_count, 2)
        self.assertEqual(len(self.requests), 3)
        self.assertTrue(all('Authorization' not in req.headers for req in self.requests))
        self.assertTrue(all(req.full_url.startswith(bot.BASE_URL + '/') for req in self.requests))

    def test_incremental_objects_can_exceed_the_old_thousand_file_limit(self):
        for index in range(1100):
            self.manifest['files'].append({'path': f'updates/blobs/{index:064x}.gz',
                                           'bytes': 1, 'sha256': hashlib.sha256(b'x').hexdigest()})
        identify(self.manifest)
        with patch.object(bot, '_assert_current'), patch.object(bot, 'https_opener', return_value=self.source_opener()):
            self.assertEqual(bot.verified_notes(self.manifest), ['Improved Party chat.', 'Saved selections & reply links.'])
        self.assertEqual(len(self.requests), 3)
        # The higher bound remains explicit; duplicate/unbounded inventories
        # cannot consume the bot's memory before public verification.
        self.manifest['files'] = [self.manifest['files'][0]] * (bot.MAX_PUBLIC_FILES + 1)
        with self.assertRaises(bot.AnnouncementError):
            bot._validate_manifest(self.manifest)

    def test_bad_live_bytes_prevent_any_announcement(self):
        for wanted in ('index.html', 'downloads/release.json', 'downloads/' + self.manifest['installer']['file']):
            with self.subTest(wanted=wanted), patch.object(bot, '_assert_current'), patch.object(
                    bot, 'https_opener', return_value=self.source_opener(lambda path, data: data + b'X' if path == wanted else data)):
                with self.assertRaises(bot.AnnouncementError):
                    bot.verified_notes(self.manifest)

    def test_changed_destination_manifest_or_unpublished_release_is_rejected(self):
        with patch.object(bot, 'https_opener') as opener:
            changed = dict(self.manifest, base_url='https://attacker.invalid')
            with self.assertRaises(bot.AnnouncementError):
                bot.verified_notes(changed)
            changed = dict(self.manifest, release_id='1.2.3-000000000000')
            with self.assertRaises(bot.AnnouncementError):
                bot.verified_notes(changed)
            with patch.object(bot, '_assert_current', side_effect=bot.AnnouncementError('Not current.')):
                with self.assertRaises(bot.AnnouncementError):
                    bot.verified_notes(self.manifest)
            opener.assert_not_called()

    def test_redirected_public_response_is_rejected_without_auth(self):
        opener = Mock()
        opener.open.return_value = Response(self.files['index.html'], 'https://elsewhere.invalid/')
        with patch.object(bot, '_assert_current'), patch.object(bot, 'https_opener', return_value=opener):
            with self.assertRaises(bot.AnnouncementError):
                bot.verified_notes(self.manifest)
        self.assertNotIn('Authorization', opener.open.call_args.args[0].headers)

    def test_public_metadata_requires_successful_packaged_validation(self):
        metadata = json.loads(self.files['downloads/release.json'])
        metadata['validation']['packaged_self_test_passed'] = False
        body = json.dumps(metadata).encode()
        self.files['downloads/release.json'] = body
        row = next(row for row in self.manifest['files'] if row['path'] == 'downloads/release.json')
        row.update(bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
        identify(self.manifest)
        with patch.object(bot, '_assert_current'), patch.object(bot, 'https_opener', return_value=self.source_opener()):
            with self.assertRaises(bot.AnnouncementError):
                bot.verified_notes(self.manifest)

    def test_current_manifest_requires_real_matching_published_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / 'releases' / self.manifest['release_id']
            target.mkdir(parents=True)
            manifests = root / 'manifests'
            manifests.mkdir()
            (manifests / (self.manifest['release_id'] + '.json')).write_text(json.dumps(self.manifest))
            current = root / 'current'
            try:
                current.symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest('Directory symlinks are unavailable on this host.')
            self.assertEqual(bot.current_manifest(root), self.manifest)
            bot._assert_current(root, self.manifest['release_id'])
            with self.assertRaises(bot.AnnouncementError):
                bot._assert_current(root, '1.2.4-000000000000')

    def test_preview_cli_never_reads_credentials_or_posts(self):
        output = io.StringIO()
        with patch.object(bot, 'current_manifest', return_value=self.manifest), \
             patch.object(bot, 'verified_notes', return_value=['Public release note.']), \
             patch.object(bot, 'DiscordClient') as client, patch.object(bot, '_config') as config, redirect_stdout(output):
            self.assertEqual(bot.main(['--current', '--preview']), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['status'], 'preview')
        self.assertEqual(result['version'], '1.2.3')
        self.assertEqual(result['payload']['allowed_mentions'], {'parse': []})
        client.assert_not_called()
        config.assert_not_called()


class PayloadAndTransportTests(unittest.TestCase):
    def test_payload_is_public_mention_safe_bounded_and_channel_nonce_is_stable(self):
        manifest, _ = release()
        payload = bot.release_payload(manifest, ['@everyone <@123> [fake](https://bad.invalid) **bold**', '😀' * 10], channel_id=CHANNEL)
        self.assertEqual(payload['allowed_mentions'], {'parse': []})
        self.assertTrue(payload['enforce_nonce'])
        self.assertLessEqual(len(payload['nonce']), 25)
        self.assertEqual(payload['embeds'], [])
        description = payload['content']
        self.assertIn('@\u200beveryone', description)
        self.assertIn(r'\[fake\]', description)
        self.assertLessEqual(len(description.encode('utf-16-le')) // 2, 2000)
        self.assertNotIn(TOKEN, json.dumps(payload))
        self.assertEqual(payload['nonce'], bot.release_payload(manifest, ['Different text'], channel_id=CHANNEL)['nonce'])
        self.assertNotEqual(payload['nonce'], bot.release_payload(manifest, ['Text'], channel_id=MESSAGE)['nonce'])

    def test_release_contains_only_version_and_patch_notes_without_generated_links(self):
        manifest, _ = release()
        payload = bot.release_payload(manifest, ['Party chat is now bright green.', 'Fixed saved channel selections.'])
        self.assertIn('**Lineage 2 Chat Translator — v1.2.3**', payload['content'])
        self.assertIn('• Party chat is now bright green.', payload['content'])
        self.assertIn('• Fixed saved channel selections.', payload['content'])
        self.assertNotIn('This release is now available', payload['content'])
        self.assertNotIn('How to update', payload['content'])
        self.assertNotIn('Open the launcher', payload['content'])
        self.assertNotIn('Install v1.2.3 & restart', payload['content'])
        self.assertNotIn('http', json.dumps(payload))
        self.assertEqual(payload['embeds'], [])

    def test_long_notes_fail_before_silent_truncation(self):
        manifest, _ = release()
        for notes in (['x' * 2000], ['😀' * 1000]):
            with self.subTest(notes=notes), self.assertRaisesRegex(bot.AnnouncementError, 'message limit'):
                bot.release_payload(manifest, notes)

    def test_edit_uses_scoped_patch_and_removes_old_embed_without_nonce(self):
        opener = Mock()
        url = bot.API_ORIGIN + f'/channels/{CHANNEL}/messages/{MESSAGE}'
        opener.open.return_value = Response(json.dumps({'id': MESSAGE}).encode(), url)
        with patch.object(bot, 'https_opener', return_value=opener):
            client = bot.DiscordClient(TOKEN)
        payload = bot.release_payload(release()[0], ['A release note.'])
        client.edit_message(CHANNEL, MESSAGE, payload)
        req = opener.open.call_args.args[0]
        self.assertEqual(req.get_method(), 'PATCH')
        self.assertEqual(req.full_url, url)
        self.assertEqual(json.loads(req.data), {'content': payload['content'], 'embeds': [], 'allowed_mentions': {'parse': []}})
        with self.assertRaises(bot.AnnouncementError):
            client.edit_message(CHANNEL, MESSAGE + '?unexpected=1', payload)
        self.assertEqual(opener.open.call_count, 1)

    def test_discord_token_only_goes_to_fixed_allowed_discord_paths(self):
        opener = Mock()
        url = bot.API_ORIGIN + '/oauth2/applications/@me'
        opener.open.return_value = Response(json.dumps({'id': APP}).encode(), url)
        with patch.object(bot, 'https_opener', return_value=opener):
            client = bot.DiscordClient(TOKEN)
        self.assertEqual(client.get('/oauth2/applications/@me'), {'id': APP})
        req = opener.open.call_args.args[0]
        self.assertEqual(req.full_url, url)
        self.assertEqual(req.headers['Authorization'], 'Bot ' + TOKEN)
        for path in ('https://evil.invalid', '//evil.invalid', '/users/@me', '/channels/1?token=x'):
            with self.assertRaises(bot.AnnouncementError):
                client.get(path)
        self.assertEqual(opener.open.call_count, 1)

    def test_transport_disables_proxy_and_redirect_handlers(self):
        opener = bot.https_opener()
        self.assertTrue(any(isinstance(handler, bot.NoRedirect) for handler in opener.handlers))
        self.assertTrue(all(not getattr(handler, 'proxies', {}) for handler in opener.handlers))
        self.assertIsNone(bot.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.invalid'))

    def test_http_network_and_invalid_body_errors_never_reveal_token(self):
        for status, ambiguous in ((401, False), (403, False), (429, False), (302, True), (500, True)):
            with self.subTest(status=status):
                opener = Mock()
                opener.open.side_effect = error.HTTPError('https://secret.invalid/' + TOKEN, status, TOKEN, {}, io.BytesIO(TOKEN.encode()))
                with patch.object(bot, 'https_opener', return_value=opener):
                    client = bot.DiscordClient(TOKEN)
                with self.assertRaises(bot.DiscordAPIError) as caught:
                    client.post_message(CHANNEL, {})
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(caught.exception.ambiguous, ambiguous)
                self.assertNotIn(TOKEN, str(caught.exception))
        for failure in (error.URLError(TOKEN), OSError(TOKEN), http.client.IncompleteRead(TOKEN.encode())):
            opener.open.side_effect = failure
            with self.assertRaises(bot.DiscordAPIError) as caught:
                client.post_message(CHANNEL, {})
            self.assertTrue(caught.exception.ambiguous)
            self.assertNotIn(TOKEN, str(caught.exception))
        opener.open.side_effect = None
        opener.open.return_value = Response(b'not json ' + TOKEN.encode(), bot.API_ORIGIN + '/channels/' + CHANNEL + '/messages')
        with self.assertRaises(bot.DiscordAPIError) as caught:
            client.post_message(CHANNEL, {})
        self.assertTrue(caught.exception.ambiguous)
        self.assertNotIn(TOKEN, str(caught.exception))


class AnnouncementTests(unittest.TestCase):
    def setUp(self):
        self.stack = self.enterContext(ExitStack())
        self.temp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = self.temp / 'public'
        self.root.mkdir()
        self.private = self.temp / 'private'
        self.private.mkdir(mode=0o700)
        self.config_path = self.private / 'config.json'
        self.state_dir = self.temp / 'state'
        self.config = {'bot_token': TOKEN, 'application_id': APP, 'guild_id': GUILD,
                       'channel_id': CHANNEL, 'channel_name': 'updates-and-announcements'}
        self.write_config()
        self.manifest, _ = release()
        self.client = Mock()
        self.client.get.side_effect = self.identity
        self.client.post_message.return_value = {'id': MESSAGE, 'channel_id': CHANNEL}
        self.client_factory = self.stack.enter_context(patch.object(bot, 'DiscordClient', return_value=self.client))
        self.notes = self.stack.enter_context(patch.object(bot, 'verified_notes', return_value=['A verified release.']))
        self.current = self.stack.enter_context(patch.object(bot, '_assert_current'))

    def write_config(self):
        self.config_path.write_text(json.dumps(self.config), encoding='utf-8')
        self.config_path.chmod(0o600)

    def identity(self, path):
        if path == '/oauth2/applications/@me':
            return {'id': APP}
        return {'id': CHANNEL, 'guild_id': GUILD, 'type': 0, 'name': 'updates-and-announcements'}

    def announce(self, manifest=None, **kwargs):
        return bot.announce_release(manifest or self.manifest, root=self.root,
                                    config_path=self.config_path, state_dir=self.state_dir, **kwargs)

    def state(self):
        return json.loads((self.state_dir / 'announcements.json').read_text())

    def test_missing_config_does_nothing_and_requires_no_network(self):
        self.config_path.unlink()
        self.assertEqual(self.announce(), {'status': 'unconfigured'})
        self.client_factory.assert_not_called()
        self.notes.assert_not_called()
        self.assertFalse(self.state_dir.exists())

    def test_success_and_duplicate_website_only_release_use_one_post(self):
        result = self.announce()
        self.assertEqual(result['status'], 'posted')
        self.assertEqual(result['message_id'], MESSAGE)
        self.assertEqual(result['message_url'], f'https://discord.com/channels/{GUILD}/{CHANNEL}/{MESSAGE}')
        state = self.state()
        entry = next(iter(state['releases'].values()))
        self.assertEqual(entry['status'], 'sent')
        self.assertNotIn(TOKEN, json.dumps(state))
        self.assertEqual(list(self.state_dir.glob('.announce-*')), [])
        changed = json.loads(json.dumps(self.manifest))
        next(row for row in changed['files'] if row['path'] == 'index.html')['sha256'] = 'f' * 64
        identify(changed)
        self.assertEqual(self.announce(changed)['status'], 'already_posted')
        self.client.post_message.assert_called_once()
        self.assertEqual(self.notes.call_count, 1)

    def test_edit_updates_confirmed_message_and_preserves_duplicate_protection(self):
        self.assertEqual(self.announce()['status'], 'posted')
        saved = self.state()
        self.client.edit_message.side_effect = lambda channel, message, payload: {
            'id': message, 'channel_id': channel, 'content': payload['content'], 'embeds': []}
        result = self.announce(edit_existing=True)
        self.assertEqual(result['status'], 'edited')
        self.assertEqual(result['message_id'], MESSAGE)
        self.assertEqual(result['embeds'], [])
        self.assertIn('A verified release.', result['content'])
        self.client.edit_message.assert_called_once()
        self.assertEqual(self.state(), saved)
        self.assertEqual(self.announce()['status'], 'already_posted')
        self.client.post_message.assert_called_once()

    def test_edit_never_creates_missing_or_unconfirmed_message(self):
        self.assertEqual(self.announce(edit_existing=True)['status'], 'not_posted')
        self.client.post_message.assert_not_called()
        self.client.edit_message.assert_not_called()
        self.notes.assert_not_called()
        self.client.post_message.side_effect = bot.DiscordAPIError(ambiguous=True)
        self.assertEqual(self.announce()['status'], 'delivery_unconfirmed')
        self.assertEqual(self.announce(edit_existing=True)['status'], 'delivery_unconfirmed')
        self.client.edit_message.assert_not_called()
        self.client.post_message.assert_called_once()

    def test_failed_or_mismatched_edit_never_replaces_or_reopens_delivery_record(self):
        self.assertEqual(self.announce()['status'], 'posted')
        saved = self.state()
        for failure, expected in ((bot.DiscordAPIError(404), 'not_edited'),
                                  (bot.DiscordAPIError(ambiguous=True), 'edit_unconfirmed'),
                                  (RuntimeError(TOKEN), 'edit_unconfirmed')):
            self.client.edit_message.side_effect = failure
            result = self.announce(edit_existing=True)
            self.assertEqual(result['status'], expected)
            self.assertNotIn(TOKEN, json.dumps(result))
        self.client.edit_message.side_effect = None
        self.client.edit_message.return_value = {'id': MESSAGE, 'channel_id': CHANNEL, 'content': 'Wrong text', 'embeds': []}
        self.assertEqual(self.announce(edit_existing=True)['status'], 'edit_unconfirmed')
        self.assertEqual(self.state(), saved)
        self.assertEqual(self.announce()['status'], 'already_posted')
        self.client.post_message.assert_called_once()

    def test_pending_is_durable_before_post_and_ambiguous_delivery_never_reposts(self):
        def ambiguous(channel, payload):
            self.assertEqual(next(iter(self.state()['releases'].values()))['status'], 'pending')
            raise bot.DiscordAPIError(ambiguous=True)
        self.client.post_message.side_effect = ambiguous
        self.assertEqual(self.announce()['status'], 'delivery_unconfirmed')
        self.assertEqual(self.announce()['status'], 'delivery_unconfirmed')
        self.client.post_message.assert_called_once()

    def test_explicit_rejection_clears_pending_and_allows_safe_later_retry(self):
        self.client.post_message.side_effect = bot.DiscordAPIError(429, ambiguous=False)
        self.assertEqual(self.announce()['status'], 'not_posted')
        self.assertEqual(self.state()['releases'], {})
        self.client.post_message.side_effect = None
        self.assertEqual(self.announce()['status'], 'posted')
        self.assertEqual(self.client.post_message.call_count, 2)

    def test_wrong_bot_or_channel_server_or_name_never_posts(self):
        for identity in ({'id': CHANNEL}, {'id': APP}):
            with self.subTest(identity=identity):
                self.client.get.side_effect = None
                self.client.get.return_value = identity
                self.assertEqual(self.announce()['status'], 'not_posted')
                self.client.post_message.assert_not_called()
        for changes in ({'guild_id': MESSAGE}, {'type': 1}, {'name': 'other-channel'}):
            def invalid(path):
                result = self.identity(path)
                if path.startswith('/channels/'):
                    result.update(changes)
                return result
            self.client.get.side_effect = invalid
            self.assertEqual(self.announce()['status'], 'not_posted')
            self.client.post_message.assert_not_called()

    def test_unverified_release_or_installer_conflict_never_posts(self):
        self.notes.side_effect = bot.AnnouncementError('Public bytes differ.')
        self.assertEqual(self.announce()['status'], 'not_posted')
        self.client_factory.assert_not_called()
        self.notes.side_effect = None
        self.assertEqual(self.announce()['status'], 'posted')
        changed = json.loads(json.dumps(self.manifest))
        changed['installer']['sha256'] = 'f' * 64
        next(row for row in changed['files'] if row['path'].endswith('.exe'))['sha256'] = 'f' * 64
        identify(changed)
        self.assertEqual(self.announce(changed)['status'], 'not_posted')
        self.client.post_message.assert_called_once()

    def test_invalid_config_secret_error_or_public_storage_never_exposes_token(self):
        self.config['bot_token'] = TOKEN + '\nmalformed'
        self.write_config()
        result = self.announce()
        self.assertEqual(result['status'], 'not_posted')
        self.assertNotIn(TOKEN, json.dumps(result))
        self.client_factory.assert_not_called()
        self.config['bot_token'] = TOKEN
        self.write_config()
        self.client.get.side_effect = RuntimeError(TOKEN)
        self.assertNotIn(TOKEN, json.dumps(self.announce()))
        self.client.get.side_effect = self.identity
        result = bot.announce_release(self.manifest, root=self.root, config_path=self.config_path,
                                      state_dir=self.root / 'private-state')
        self.assertEqual(result['status'], 'not_posted')
        self.assertFalse((self.root / 'private-state').exists())

    def test_config_cannot_expand_posting_scope_beyond_authorized_server_and_channel(self):
        for guild, name in ((MESSAGE, 'updates-and-announcements'), (GUILD, None), (GUILD, 'general')):
            with self.subTest(guild=guild, name=name):
                self.config['guild_id'] = guild
                if name is None:
                    self.config.pop('channel_name', None)
                else:
                    self.config['channel_name'] = name
                self.write_config()
                self.assertEqual(self.announce()['status'], 'not_posted')
                self.notes.assert_not_called()
                self.client_factory.assert_not_called()
                self.assertFalse(self.state_dir.exists())

    def test_existing_lock_serializes_posts_and_state_write_is_atomic(self):
        bot._private_directory(self.state_dir, create=True)
        with bot._state_lock(self.state_dir):
            self.assertEqual(self.announce()['status'], 'not_posted')
            self.client.post_message.assert_not_called()
        self.assertEqual(self.announce()['status'], 'posted')
        previous = (self.state_dir / 'announcements.json').read_bytes()
        with patch.object(bot.os, 'replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                bot._save_state(self.state_dir, {'schema_version': 1, 'releases': {}})
        self.assertEqual((self.state_dir / 'announcements.json').read_bytes(), previous)
        self.assertFalse(list(self.state_dir.glob('.announce-*')))

    def test_corrupt_state_fails_closed_instead_of_reposting(self):
        bot._private_directory(self.state_dir, create=True)
        path = self.state_dir / 'announcements.json'
        path.write_text('{broken')
        path.chmod(0o600)
        self.assertEqual(self.announce()['status'], 'not_posted')
        self.client.post_message.assert_not_called()

    def test_post_accepted_but_state_commit_fails_leaves_pending_for_review(self):
        original = bot._save_state
        calls = 0
        def fail_commit(directory, state):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('commit failure')
            return original(directory, state)
        with patch.object(bot, '_save_state', side_effect=fail_commit):
            result = self.announce()
        self.assertEqual(result['status'], 'posted_state_unconfirmed')
        self.assertEqual(result['message_id'], MESSAGE)
        self.assertEqual(result['message_url'], f'https://discord.com/channels/{GUILD}/{CHANNEL}/{MESSAGE}')
        self.assertEqual(next(iter(self.state()['releases'].values()))['status'], 'pending')
        self.assertEqual(self.announce()['status'], 'delivery_unconfirmed')
        self.client.post_message.assert_called_once()

    def test_confirmed_post_with_unconfirmed_ledger_is_a_nonzero_cli_attention_result(self):
        output = io.StringIO()
        with patch.object(bot, 'current_manifest', return_value=self.manifest), \
             patch.object(bot, 'announce_release', return_value={'status': 'posted_state_unconfirmed', 'message_id': MESSAGE}), \
             redirect_stdout(output):
            self.assertEqual(bot.main(['--current']), 2)
        self.assertEqual(json.loads(output.getvalue())['message_id'], MESSAGE)


if __name__ == '__main__':
    unittest.main()
