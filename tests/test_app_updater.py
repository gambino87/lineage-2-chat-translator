"""Updater protocol/filesystem checks; no network, apps, installers or services."""
import hashlib
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib import error, request

import app_updater as updater
import secure_storage


class Response(io.BytesIO):
    def __init__(self, data, *, url=updater.RELEASE_URL, length=None, status=200):
        super().__init__(data)
        self.url, self.status = url, status
        self.headers = {} if length is None else {'Content-Length': str(length)}

    def geturl(self):
        return self.url


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.folder = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.folder)
        # Scope ACL tests to a disposable private tree. Do not repair or depend
        # on this machine's real profile/drive permissions.
        self.enterContext(patch.object(secure_storage, '_ancestor_paths', side_effect=lambda path: [
            parent for parent in reversed(path.parents) if parent == self.root or self.root in parent.parents]))
        with secure_storage.guard_private_tree(self.root, authorize_existing=lambda _: True):
            pass
        self.preferences = self.root / 'Lineage2ChatTranslator'
        self.preferences.mkdir()
        self.executable = self.root / 'Installed app' / 'Lineage2ChatTranslator.exe'
        self.executable.parent.mkdir()
        self.executable.write_bytes(b'synthetic current application')
        self.binary = b'MZ' + b'synthetic installer bytes.' * 100
        metadata = json.dumps(self.metadata()).encode()
        self.release = updater.UpdateRelease('99.8.7', 'Lineage-2-Chat-Translator-99.8.7-Setup.exe',
                                             len(self.binary), hashlib.sha256(self.binary).hexdigest(),
                                             metadata, self.signature(metadata))
        # This protocol fixture is deliberately not a signing implementation.
        # Native RSA/PSS verification has separate Windows integration tests.
        self.verifier = self.enterContext(patch.object(updater, 'verify_release_signature',
                                                       side_effect=self.verify_fixture))
        self.enterContext(patch.object(updater, 'preferences_directory', return_value=self.preferences))
        self.enterContext(patch.object(updater, 'update_in_progress', return_value=False))

    def metadata(self, version='99.8.7'):
        return {'product': updater.PRODUCT, 'platform': 'windows-x64', 'version': version,
                'installer': {'file': f'Lineage-2-Chat-Translator-{version}-Setup.exe',
                              'bytes': len(self.binary), 'sha256': hashlib.sha256(self.binary).hexdigest()},
                'validation': {'packaged_self_test_passed': True}}

    @staticmethod
    def signature(data):
        return hashlib.sha256(data).digest() * 12

    def verify_fixture(self, data, signature):
        if type(data) is not bytes or not data or signature != self.signature(data):
            raise updater.SignatureError('synthetic signature mismatch')

    def check(self, document, current='1.6.0'):
        data = document if isinstance(document, bytes) else json.dumps(document).encode()
        with patch.object(updater, '_open', side_effect=[Response(data), Response(self.signature(data))]) as fetch:
            result = updater.check_for_update(current)
        self.assertEqual([call.args[0] for call in fetch.call_args_list],
                         [updater.RELEASE_URL, updater.SIGNATURE_URL])
        return result

    def prepare(self, binary=None, *, progress=None, cancelled=None, content_length=None):
        content = self.binary if binary is None else binary
        with patch.object(updater, '_installed_executable', return_value=self.executable), \
             patch.object(updater, '_open', return_value=Response(content, length=content_length)):
            return updater.prepare_update(self.release, progress=progress, cancelled=cancelled)

    def test_versions_compare_numerically_and_never_offer_downgrade(self):
        self.assertEqual(self.check(self.metadata('1.10.0'), '1.9.9').version, '1.10.0')
        self.assertIsNone(self.check(self.metadata('1.6.0'), '1.6.0'))
        self.assertIsNone(self.check(self.metadata('1.5.7'), '1.6.0'))

    def test_invalid_versions_products_platforms_and_checks_rejected(self):
        for changes in ({'version': 'latest'}, {'version': '1.7'}, {'version': '01.7.0'},
                        {'version': '1.7.0/../evil'}, {'product': 'Different App'},
                        {'platform': 'linux-x64'}, {'validation': {}},
                        {'validation': {'packaged_self_test_passed': False}}):
            with self.subTest(changes=changes):
                document = self.metadata()
                document.update(changes)
                with self.assertRaises(updater.UpdateError):
                    self.check(document)

    def test_installer_filename_size_hash_are_strict(self):
        for changes in ({'file': '../other.exe'}, {'file': 'https://other.example/evil.exe'},
                        {'file': 'Lineage-2-Chat-Translator-1.7.0-Setup.exe'}, {'bytes': True},
                        {'bytes': 0}, {'bytes': updater.MAX_INSTALLER_BYTES + 1},
                        {'sha256': 'not a hash'}, {'sha256': 'a' * 65}):
            with self.subTest(changes=changes):
                document = self.metadata()
                document['installer'].update(changes)
                with self.assertRaises(updater.UpdateError):
                    self.check(document)

    def test_duplicate_or_excessive_metadata_rejected(self):
        valid = json.dumps(self.metadata()).encode()
        duplicate = valid.replace(b'{', b'{"version":"0.0.0",', 1)
        for data in [duplicate, b'[]', b'{broken', b' ' * (updater.MAX_METADATA_BYTES + 1)]:
            with self.subTest(size=len(data)), self.assertRaises(updater.UpdateError):
                self.check(data)

    def test_metadata_check_does_not_stage_or_execute(self):
        with patch.object(updater, 'prepare_update') as prepare, patch.object(updater.subprocess, 'Popen') as launch:
            self.assertEqual(self.check(self.metadata()), self.release)
        prepare.assert_not_called()
        launch.assert_not_called()
        self.assertFalse((self.preferences / 'updates').exists())

    def test_requests_use_only_fixed_https_origin(self):
        for url in ('http://40.160.140.227/downloads/release.json',
                    'https://evil.example/downloads/release.json',
                    updater.BASE_URL + '/downloads/../private',
                    updater.BASE_URL + '/downloads/release.json?redirect=evil'):
            with self.subTest(url=url), self.assertRaises(updater.UpdateError), \
                 patch.object(updater, '_opener') as opener:
                updater._open(url)
            opener.assert_not_called()

    def test_redirects_are_rejected_including_same_host(self):
        handler = updater._NoRedirect()
        for target in ('https://evil.example/setup.exe', updater.BASE_URL + '/another'):
            with self.subTest(target=target), self.assertRaises(updater.UpdateError):
                handler.redirect_request(request.Request(updater.RELEASE_URL), None, 302, '', {}, target)
        with patch.object(updater, '_opener') as opener:
            opener.return_value.open.return_value = Response(b'{}', url='https://evil.example')
            with self.assertRaises(updater.UpdateError):
                updater._open(updater.RELEASE_URL)

    def test_network_failure_is_recoverable(self):
        with patch.object(updater, '_opener') as opener:
            opener.return_value.open.side_effect = error.URLError('offline')
            with self.assertRaisesRegex(updater.UpdateError, 'internet connection'):
                updater.check_for_update('1.6.0')

    def test_update_request_sends_only_fixed_headers_without_user_content(self):
        with patch.object(updater, '_opener') as opener:
            response = Response(b'{}')
            opener.return_value.open.return_value = response
            self.assertIs(updater._open(updater.RELEASE_URL), response)
        outbound = opener.return_value.open.call_args.args[0]
        self.assertEqual(outbound.full_url, updater.RELEASE_URL)
        self.assertIsNone(outbound.data)
        self.assertEqual(dict(outbound.header_items()), {
            'Accept': 'application/json', 'User-agent': f'Lineage2ChatTranslator/{updater.VERSION}'})

    def test_local_record_read_stays_bounded_if_file_grows_after_size_check(self):
        path = self.preferences / 'ready.json'
        path.write_text('{}', encoding='utf-8')
        maximum = 8
        content = io.BytesIO(b' ' * (maximum + 1000))
        with patch.object(Path, 'open', return_value=content), \
             self.assertRaisesRegex(updater.UpdateError, 'too large'):
            updater._read_json(path, maximum)
        # Closing occurs even when an oversized record is rejected.
        self.assertTrue(content.closed)

    def test_local_record_accepts_utf8_bom_at_exact_byte_limit(self):
        path = self.preferences / 'ready.json'
        raw = b'\xef\xbb\xbf{"ok":true}'
        path.write_bytes(raw)
        self.assertEqual(updater._read_json(path, len(raw)), {'ok': True})

    def test_source_run_cannot_install(self):
        with patch.object(updater.sys, 'frozen', False, create=True):
            supported, reason = updater.installation_support()
        self.assertFalse(supported)
        self.assertIn('installed Windows app', reason)
        with patch.object(updater.sys, 'frozen', False, create=True), patch.object(updater, '_open') as fetch:
            with self.assertRaises(updater.UpdateError):
                updater.prepare_update(self.release)
        fetch.assert_not_called()

    def test_verified_download_stages_fixed_runner_and_job_without_launch(self):
        progress = Mock()
        with patch.object(updater.subprocess, 'Popen') as launch:
            prepared = self.prepare(progress=progress, content_length=len(self.binary))
        launch.assert_not_called()
        self.assertEqual((prepared.stage / 'installer.exe').read_bytes(), self.binary)
        self.assertEqual((prepared.stage / 'release.json').read_bytes(), self.release.metadata)
        self.assertEqual((prepared.stage / 'release.sig').read_bytes(), self.release.signature)
        self.assertFalse((prepared.stage / 'installer.part').exists())
        self.assertEqual((prepared.stage / 'apply-update.ps1').read_bytes(), updater.RUNNER_PATH.read_bytes())
        job = json.loads((prepared.stage / 'job.json').read_text())
        self.assertEqual(job['executable'], str(self.executable))
        self.assertEqual(job['installer_sha256'], self.release.sha256)
        self.assertEqual(job['version'], self.release.version)
        progress.assert_called_with(len(self.binary), len(self.binary))
        updater.discard_update(prepared)
        self.assertFalse(prepared.stage.exists())

    def test_download_hash_size_and_content_length_fail_before_handoff(self):
        for content, length in [(self.binary + b'x', None), (self.binary[:-1], None),
                                (b'MZ' + b'x' * (len(self.binary) - 2), None),
                                (self.binary, 1)]:
            with self.subTest(length=length, size=len(content)), self.assertRaises(updater.UpdateError), \
                 patch.object(updater.subprocess, 'Popen') as launch:
                self.prepare(content, content_length=length)
            launch.assert_not_called()
            root = self.preferences / 'updates'
            self.assertTrue(not root.exists() or not list(root.iterdir()))

    def test_cancel_during_download_removes_only_owned_partial(self):
        cancel = threading.Event()
        sibling = self.preferences / 'settings.json'
        sibling.write_bytes(b'preferences stay')
        with self.assertRaises(updater.UpdateCancelled):
            self.prepare(progress=lambda *_: cancel.set(), cancelled=cancel)
        self.assertEqual(sibling.read_bytes(), b'preferences stay')
        self.assertEqual(list((self.preferences / 'updates').iterdir()), [])

    def test_cancel_before_start_does_not_download_or_create_cache(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(updater, '_installed_executable', return_value=self.executable), \
             patch.object(updater, '_open') as fetch, self.assertRaises(updater.UpdateCancelled):
            updater.prepare_update(self.release, cancelled=cancel)
        fetch.assert_not_called()
        self.assertFalse((self.preferences / 'updates').exists())

    def test_cleanup_refuses_unknown_files_and_never_recurses(self):
        prepared = self.prepare()
        unrelated = prepared.stage / 'my-note.txt'
        unrelated.write_bytes(b'keep')
        with self.assertRaisesRegex(updater.UpdateError, 'Unrecognized'):
            updater.discard_update(prepared)
        self.assertTrue((prepared.stage / 'installer.exe').exists())
        self.assertEqual(unrelated.read_bytes(), b'keep')
        updater.cleanup_update_cache(strict=False)
        self.assertTrue(unrelated.exists())

    def test_cleanup_refuses_unowned_stage_and_all_active_updates(self):
        root = self.preferences / 'updates'
        root.mkdir()
        unrelated = root / 'user-folder'
        unrelated.mkdir()
        (unrelated / 'keep').write_text('hello')
        with self.assertRaises(updater.UpdateError):
            updater.cleanup_update_cache()
        self.assertTrue((unrelated / 'keep').exists())
        with patch.object(updater, 'update_in_progress', return_value=True):
            with self.assertRaisesRegex(updater.UpdateError, 'still running'):
                updater.cleanup_update_cache()

    def test_best_effort_cleanup_never_blocks_startup_on_bad_cache(self):
        (self.preferences / 'updates').write_bytes(b'not a directory')
        updater.cleanup_update_cache(strict=False)
        self.assertEqual((self.preferences / 'updates').read_bytes(), b'not a directory')
        updater.cleanup_update_cache(self.root / 'unexpected-name', strict=False)

    def test_cleanup_rejects_redirected_child_when_symlinks_available(self):
        prepared = self.prepare()
        victim = self.root / 'outside.txt'
        victim.write_text('keep me')
        target = prepared.stage / 'installer.log'
        try:
            target.symlink_to(victim)
        except OSError:
            self.skipTest('File symlink creation unavailable')
        with self.assertRaises(updater.UpdateError):
            updater.discard_update(prepared)
        self.assertEqual(victim.read_text(), 'keep me')

    def fake_launch(self, prepared, *, nonce=None, pid=32123):
        process = Mock(pid=pid)
        process.poll.return_value = None
        owner = json.loads((prepared.stage / 'owner.json').read_text())
        (prepared.stage / 'ready.json').write_text(json.dumps({'nonce': nonce or owner['nonce'], 'pid': pid}))
        return process

    def test_handoff_commits_only_after_matching_ready_and_uses_argument_list(self):
        prepared = self.prepare()
        process = Mock(pid=32123)
        process.poll.return_value = None
        def start(*args, **kwargs):
            self.assertFalse((prepared.stage / 'commit.json').exists())
            owner = json.loads((prepared.stage / 'owner.json').read_text())
            (prepared.stage / 'ready.json').write_text(json.dumps({'nonce': owner['nonce'], 'pid': process.pid}))
            return process
        with patch.object(updater, '_installed_executable', return_value=self.executable), \
             patch.object(updater, '_powershell', return_value=Path('C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe')), \
             patch.object(updater.subprocess, 'Popen', side_effect=start) as launch:
            updater.launch_update(prepared)
        args = launch.call_args.args[0]
        self.assertIsInstance(args, list)
        self.assertNotIn('-Command', args)
        self.assertEqual(args[-2:], ['-JobPath', str(prepared.stage / 'job.json')])
        self.assertNotIn('shell', launch.call_args.kwargs)
        self.assertEqual(launch.call_args.kwargs['cwd'], self.preferences)
        self.assertFalse(launch.call_args.kwargs['cwd'].is_relative_to(prepared.stage))
        self.assertTrue((prepared.stage / 'commit.json').is_file())
        self.assertFalse((prepared.stage / 'commit.pending').exists())
        process.terminate.assert_not_called()

    def test_verified_helper_cannot_be_rewritten_or_renamed_at_launch(self):
        prepared = self.prepare()
        helper = prepared.stage / 'apply-update.ps1'
        original = helper.read_bytes()
        def start(*args, **kwargs):
            # These harmless writes reproduce the old last-hash-to-Popen gap.
            with self.assertRaises(OSError):
                helper.write_bytes(b'changed fixture helper; never executed')
            with self.assertRaises(OSError):
                helper.rename(prepared.stage / 'renamed.ps1')
            with self.assertRaises(OSError):
                prepared.stage.rename(prepared.stage.with_name('renamed-stage'))
            return self.fake_launch(prepared)
        with patch.object(updater, '_installed_executable', return_value=self.executable), \
             patch.object(updater, '_powershell', return_value=Path('C:/Windows/powershell.exe')), \
             patch.object(updater.subprocess, 'Popen', side_effect=start):
            updater.launch_update(prepared)
        self.assertEqual(helper.read_bytes(), original)
        # The owner can edit/delete normally after the handshake releases locks.
        helper.write_bytes(b'owner retains full control')
        helper.unlink()

    def test_stage_files_are_private_and_owner_remains_in_full_control(self):
        prepared = self.prepare()
        api = secure_storage._Windows()
        for path in (prepared.stage, prepared.stage / 'installer.exe', prepared.stage / 'apply-update.ps1'):
            handle = api.open(path)
            try:
                self.assertTrue(api.private(api.security(handle), path.is_dir(), root=path.is_dir()))
            finally:
                api.k.CloseHandle(handle)
        updater.discard_update(prepared)
        self.assertFalse(prepared.stage.exists())

    def test_hardlinked_helper_is_rejected_without_altering_external_file(self):
        prepared = self.prepare()
        helper = prepared.stage / 'apply-update.ps1'
        outside = self.root / 'outside-script.ps1'
        outside.write_bytes(helper.read_bytes())
        helper.unlink()
        os.link(outside, helper)
        with patch.object(updater, '_installed_executable', return_value=self.executable), \
             patch.object(updater.subprocess, 'Popen') as launch, self.assertRaises(OSError):
            updater.launch_update(prepared)
        launch.assert_not_called()
        self.assertEqual(outside.read_bytes(), updater.RUNNER_PATH.read_bytes())

    def test_handoff_timeout_cancels_own_helper_without_commit(self):
        prepared = self.prepare()
        process = Mock(pid=32123)
        process.poll.return_value = None
        with patch.object(updater, '_installed_executable', return_value=self.executable), \
             patch.object(updater, '_powershell', return_value=Path('C:/Windows/powershell.exe')), \
             patch.object(updater.subprocess, 'Popen', return_value=process), \
             patch.object(updater, 'READY_TIMEOUT', 0), self.assertRaises(updater.UpdateError):
            updater.launch_update(prepared)
        process.terminate.assert_called_once()
        self.assertFalse(prepared.stage.exists())

    def test_wrong_helper_identity_never_commits(self):
        prepared = self.prepare()
        def start(*args, **kwargs):
            return self.fake_launch(prepared, nonce='wrong')
        with patch.object(updater, '_installed_executable', return_value=self.executable), \
             patch.object(updater, '_powershell', return_value=Path('C:/Windows/powershell.exe')), \
             patch.object(updater.subprocess, 'Popen', side_effect=start), self.assertRaises(updater.UpdateError):
            updater.launch_update(prepared)
        self.assertFalse(prepared.stage.exists())

    def test_confirmation_is_complete_before_atomic_publication(self):
        prepared = self.prepare()
        process = Mock(pid=32123)
        process.poll.return_value = None
        def start(*args, **kwargs):
            owner = json.loads((prepared.stage / 'owner.json').read_text())
            (prepared.stage / 'ready.json').write_text(json.dumps({'nonce': owner['nonce'], 'pid': process.pid}))
            return process
        original_replace = Path.replace
        publications = []
        def replace(path, target):
            if path.name == 'commit.pending':
                self.assertFalse(Path(target).exists())
                confirmation = json.loads(path.read_text())
                self.assertEqual(confirmation['nonce'], prepared.stage.name[7:])
                publications.append(confirmation)
            return original_replace(path, target)
        with patch.object(updater, '_installed_executable', return_value=self.executable), \
             patch.object(updater, '_powershell', return_value=Path('C:/Windows/powershell.exe')), \
             patch.object(updater.subprocess, 'Popen', side_effect=start), patch.object(Path, 'replace', replace):
            updater.launch_update(prepared)
        self.assertEqual(len(publications), 1)

    def test_download_has_an_overall_deadline_and_cleans_partial(self):
        with patch.object(updater, 'MAX_DOWNLOAD_SECONDS', 0), self.assertRaisesRegex(updater.UpdateError, 'too long'):
            self.prepare()
        self.assertEqual(list((self.preferences / 'updates').iterdir()), [])

    def test_stage_directory_removal_failure_retains_ownership_for_retry(self):
        prepared = self.prepare()
        original_rmdir = Path.rmdir
        def remove(path):
            if path == prepared.stage:
                raise PermissionError('synthetic directory handle')
            return original_rmdir(path)
        with patch.object(Path, 'rmdir', remove), self.assertRaises(PermissionError):
            updater.discard_update(prepared)
        self.assertEqual(list(prepared.stage.iterdir()), [prepared.stage / 'owner.json'])
        updater.discard_update(prepared)
        self.assertFalse(prepared.stage.exists())

    def test_changed_installer_or_job_or_script_is_never_launched(self):
        for filename in ['installer.exe', 'job.json', 'apply-update.ps1', 'release.json', 'release.sig']:
            with self.subTest(filename=filename):
                prepared = self.prepare()
                path = prepared.stage / filename
                if filename == 'job.json':
                    value = json.loads(path.read_text())
                    value['executable'] = str(self.root / 'evil.exe')
                    path.write_text(json.dumps(value))
                else:
                    path.write_bytes(path.read_bytes() + b'changed')
                with patch.object(updater, '_installed_executable', return_value=self.executable), \
                     patch.object(updater.subprocess, 'Popen') as launch, self.assertRaises(updater.UpdateError):
                    updater.launch_update(prepared)
                launch.assert_not_called()
                updater.discard_update(prepared)

    def test_untrusted_signature_blocks_before_metadata_is_interpreted(self):
        for signature in (b'', b'wrong key or modified signature', b'x' * updater.SIGNATURE_BYTES):
            with self.subTest(signature_length=len(signature)), \
                 patch.object(updater, '_open', side_effect=[Response(b'{host-controlled invalid json'), Response(signature)]), \
                 patch.object(updater, '_json_bytes') as parse, \
                 self.assertRaisesRegex(updater.UpdateError, 'signature'):
                updater.check_for_update()
            parse.assert_not_called()

    def test_signature_response_is_bounded(self):
        with patch.object(updater, '_open', side_effect=[Response(self.release.metadata),
                                                       Response(b'x' * (updater.SIGNATURE_BYTES + 1))]), \
             self.assertRaisesRegex(updater.UpdateError, 'too large'):
            updater.check_for_update()

    def test_construction_and_changed_fields_do_not_convey_trust(self):
        variants = [replace(self.release, metadata=b'', signature=b''),
                    replace(self.release, sha256='a' * 64),
                    replace(self.release, bytes=self.release.bytes + 1),
                    replace(self.release, version='99.8.8', file='Lineage-2-Chat-Translator-99.8.8-Setup.exe'),
                    replace(self.release, metadata=self.release.metadata + b' '),
                    replace(self.release, signature=b'x' * updater.SIGNATURE_BYTES)]
        for release in variants:
            with self.subTest(release=release.version), patch.object(updater, '_open') as fetch, \
                 patch.object(updater, '_installed_executable') as installed, \
                 self.assertRaises(updater.UpdateError):
                updater.prepare_update(release)
            fetch.assert_not_called()
            installed.assert_not_called()

    def test_signed_old_release_cannot_be_prepared(self):
        data = json.dumps(self.metadata('0.0.1')).encode()
        old = updater._signed_release(data, self.signature(data))
        with patch.object(updater, '_open') as fetch, self.assertRaisesRegex(updater.UpdateError, 'not newer'):
            updater.prepare_update(old)
        fetch.assert_not_called()

    def test_different_valid_signed_release_cannot_replace_staged_metadata(self):
        prepared = self.prepare()
        data = json.dumps(self.metadata('99.8.8')).encode()
        (prepared.stage / 'release.json').write_bytes(data)
        (prepared.stage / 'release.sig').write_bytes(self.signature(data))
        with patch.object(updater.subprocess, 'Popen') as launch, \
             self.assertRaisesRegex(updater.UpdateError, 'signed update details changed'):
            updater.launch_update(prepared)
        launch.assert_not_called()
        updater.discard_update(prepared)

    def test_receipt_consumed_once_without_changing_preferences(self):
        receipt = self.preferences / 'update-result.json'
        value = {'app_id': updater.APP_ID, 'status': 'success', 'version': '99.8.7', 'message': 'Done'}
        receipt.write_text(json.dumps(value))
        self.assertEqual(updater.consume_update_result(self.preferences),
                         {'status': 'success', 'version': '99.8.7', 'message': 'Done'})
        self.assertIsNone(updater.consume_update_result(self.preferences))

    def test_bad_receipt_cannot_brick_normal_startup(self):
        for data in [b'{truncated', b'x' * 20000, b'{}', b'[]']:
            with self.subTest(size=len(data)):
                path = self.preferences / 'update-result.json'
                path.write_bytes(data)
                result = updater.consume_update_result(self.preferences)
                self.assertEqual(result['status'], 'error')
                self.assertFalse(path.exists())
                self.assertIsNone(updater.consume_update_result(self.preferences))

    def test_already_installed_cleanup_failure_points_to_same_installer_including_older_runner(self):
        legacy = ('The installer did not complete (code 4). '
                  'The installed translator will reopen; you can retry the update from Settings.')
        for running_version, repaired in (("1.7.3", True), ("1.7.2", False)):
            with self.subTest(running_version=running_version), patch.object(updater, 'VERSION', running_version):
                receipt = self.preferences / 'update-result.json'
                receipt.write_text(json.dumps({'app_id': updater.APP_ID, 'status': 'error',
                                              'version': '1.7.3', 'message': legacy}))
                result = updater.consume_update_result(self.preferences)
                self.assertIn('code 4', result['message'])
                if repaired:
                    self.assertIn('rerun this version\'s installer', result['message'])
                    self.assertIn(updater.BASE_URL, result['message'])
                    self.assertNotIn('retry the update from Settings', result['message'])
                else:
                    self.assertEqual(result['message'], legacy)
                self.assertIsNone(updater.consume_update_result(self.preferences))

    def test_runner_uses_silent_upgrade_without_model_setup_or_forced_closes(self):
        source = updater.RUNNER_PATH.read_text(encoding='utf-8')
        for flag in ['/SP-', '/SILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/NOCLOSEAPPLICATIONS',
                     '/NORESTARTAPPLICATIONS', '/AUTOUPDATE=1', '/RESTARTEXITCODE=']:
            self.assertIn(flag, source)
        self.assertNotIn('Invoke-Expression', source.replace('Invoke-Expression or eval', ''))
        self.assertNotIn('Remove-Item -Recurse', source)
        self.assertIn('$restartPermitted = $false', source)
        self.assertIn('$restartRequiredExitCode = 3010', source)
        self.assertIn('$committed -and $parentExited -and $restartPermitted', source)
        self.assertLess(source.index('$originalProcess.WaitForExit($parentExitTimeoutMs)'), source.index('$installerProcess = Start-VerifiedProcess'))
        self.assertLess(source.index('if ($helperMutex)'), source.index('if ($restartExecutable)'))


if __name__ == '__main__':
    unittest.main()
