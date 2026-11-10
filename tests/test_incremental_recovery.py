"""Installer recovery and uninstall receipt handoff use disposable fixtures."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import app_updater as updater
import installer_main as entrypoint
import secure_storage
import uninstall_data
from tests import test_incremental_client as client_support
from tests.test_incremental_update import VERSION, encode


class IncrementalRecoveryTests(unittest.TestCase):
    setUp = client_support.IncrementalClientTests.setUp
    metadata = client_support.IncrementalClientTests.metadata
    signature = staticmethod(client_support.IncrementalClientTests.signature)
    verify_fixture = client_support.IncrementalClientTests.verify_fixture
    configure = client_support.IncrementalClientTests.configure

    def stage(self):
        self.configure()
        with patch.object(updater, '_installed_executable', return_value=self.executable), \
             patch.object(updater, '_open', side_effect=self.response):
            prepared = updater.prepare_update(self.release)
        job = json.loads((prepared.stage / 'job.json').read_text())
        self.journal = {'app_id': updater.APP_ID, 'schema_version': 1, 'nonce': job['nonce'],
                        'state': 'applying', 'version': VERSION, 'entries': []}
        (prepared.stage / 'transaction.json').write_bytes(encode(self.journal))
        (prepared.stage / 'rollback-00000.bak').write_bytes(b'original recovery bytes')
        return prepared

    def response(self, url, *_):
        from tests.test_app_updater import Response
        return Response(self.downloads[url], url=url)

    def repair(self):
        with patch.object(updater, '_installed_executable', return_value=self.executable), \
             patch.object(updater, 'VERSION', VERSION):
            return updater.finish_installer_repair(self.preferences)

    def test_full_installer_retires_backups_after_explicit_verified_repair(self):
        prepared = self.stage()
        with self.assertRaises(updater.UpdateError):
            updater.ensure_no_interrupted_update(self.preferences)
        report = self.repair()
        self.assertEqual(report['repaired_updates'], 1)
        self.assertFalse(prepared.stage.exists())
        updater.ensure_no_interrupted_update(self.preferences)

    def test_pending_only_and_partial_journal_are_repairable_after_full_install(self):
        for pending in (encode({'partial': True}), b'{interrupted'):
            with self.subTest(pending=pending):
                prepared = self.stage()
                (prepared.stage / 'transaction.json').unlink()
                (prepared.stage / 'transaction.pending').write_bytes(pending)
                self.assertEqual(self.repair()['repaired_updates'], 1)
                self.assertFalse(prepared.stage.exists())

    def test_unidentified_sidecar_is_preserved_without_blocking_full_repair(self):
        prepared = self.stage()
        relative = '_internal/changed.txt'
        temporary = relative + '.l2ct-' + self.journal['nonce']
        path = self.executable.parent / temporary
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'incomplete unidentifiable temporary content')
        self.journal['entries'] = [{'path': relative, 'temporary': temporary, 'temporary_id': ''}]
        (prepared.stage / 'transaction.json').write_bytes(encode(self.journal))
        report = self.repair()
        self.assertEqual(report['preserved_temporary_files'], [str(path)])
        self.assertEqual(path.read_bytes(), b'incomplete unidentifiable temporary content')
        self.assertFalse(prepared.stage.exists())
        updater.ensure_no_interrupted_update(self.preferences)

    def test_identified_sidecar_removed_by_matching_native_identity(self):
        prepared = self.stage()
        relative = '_internal/changed.txt'
        temporary = relative + '.l2ct-' + self.journal['nonce']
        path = self.executable.parent / temporary
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'owned incomplete temporary content')
        identity = secure_storage.path_identity(path)
        self.journal['entries'] = [{'path': relative, 'temporary': temporary,
                                    'temporary_id': ':'.join(map(str, identity))}]
        (prepared.stage / 'transaction.json').write_bytes(encode(self.journal))
        self.assertEqual(self.repair()['preserved_temporary_files'], [])
        self.assertFalse(path.exists())

    def test_wrong_installation_or_tampered_manifest_preserves_backups(self):
        prepared = self.stage()
        (prepared.stage / 'files.json').write_bytes(b'{}')
        with self.assertRaises(updater.UpdateError):
            self.repair()
        self.assertEqual((prepared.stage / 'rollback-00000.bak').read_bytes(), b'original recovery bytes')

    def test_exact_identity_delete_rejects_raced_file(self):
        target = self.root / 'synthetic-owned-temp'
        target.write_bytes(b'keep this file')
        wrong = list(secure_storage.path_identity(target))
        wrong[-1] += 1
        with self.assertRaises(secure_storage.StorageError):
            secure_storage.safe_remove_tree(target, expected_identity=wrong)
        self.assertEqual(target.read_bytes(), b'keep this file')

    def installed_receipts(self):
        self.configure()
        (self.preferences / 'installed-files.json').write_bytes(self.manifest_data)
        (self.preferences / 'installed-release.json').write_bytes(self.release.metadata)
        (self.preferences / 'installed-release.sig').write_bytes(self.release.signature)

    def test_uninstall_exports_authenticated_inventory_and_keeps_receipts_for_retry(self):
        self.installed_receipts()
        folder = self.root / 'is-fixture.tmp'
        folder.mkdir()
        with patch.object(updater, 'VERSION', VERSION):
            self.assertTrue(updater.prepare_incremental_uninstall(folder / 'result.txt', self.preferences))
        plan = json.loads((folder / 'l2-incremental-uninstall.json').read_text())
        self.assertEqual({row['path'] for row in plan['files']}, set(self.target))
        self.assertEqual(plan['preferences'], str(self.preferences))
        self.assertEqual(set(plan['receipts']), set(updater.INSTALLED_RECEIPTS))
        self.assertTrue(all((self.preferences / name).is_file() for name in updater.INSTALLED_RECEIPTS))
        self.assertEqual((folder / 'l2-incremental-uninstall.ps1').read_bytes(),
                         (updater.RUNNER_PATH.parent / 'cleanup-program-files.ps1').read_bytes())

    def test_uninstall_rejects_tampered_receipt_and_untrusted_temp_location(self):
        self.installed_receipts()
        with patch.object(updater, 'VERSION', VERSION), self.assertRaises(updater.UpdateError):
            updater.prepare_incremental_uninstall(self.root / 'result.txt', self.preferences)
        (self.preferences / 'installed-files.json').write_bytes(b'{}')
        folder = self.root / 'is-fixture.tmp'
        folder.mkdir()
        with patch.object(updater, 'VERSION', VERSION), self.assertRaises(updater.UpdateError):
            updater.prepare_incremental_uninstall(folder / 'result.txt', self.preferences)
        self.assertEqual(list(folder.iterdir()), [])

    def test_managed_data_cleanup_keeps_receipts_for_external_cleanup_retry(self):
        self.installed_receipts()
        (self.preferences / 'settings.json').write_text('{}')
        uninstall_data.uninstall_data(self.preferences)
        self.assertFalse((self.preferences / 'settings.json').exists())
        self.assertTrue(all((self.preferences / name).is_file() for name in updater.INSTALLED_RECEIPTS))

    def test_repair_entrypoint_is_headless_and_does_not_start_services(self):
        with patch.object(entrypoint, 'finish_installer_repair', return_value={'ok': True}) as repair, \
             patch.object(entrypoint.tk, 'Tk') as window, patch.object(entrypoint, 'RuntimeManager') as services:
            self.assertEqual(entrypoint.main(['--finish-update-repair']), 0)
        repair.assert_called_once()
        window.assert_not_called()
        services.assert_not_called()


if __name__ == '__main__':
    unittest.main()
