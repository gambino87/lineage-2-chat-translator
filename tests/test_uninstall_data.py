"""Uninstall tests use temporary fixtures and mocked service operations only."""
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import uninstall_data as cleanup
from installer_runtime import ServiceOwnershipMismatch, SetupError


class UninstallDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.preferences = self.base / "roaming" / "Lineage2ChatTranslator"
        self.data = self.base / "model data"
        self.manager_patch = patch.object(cleanup, "RuntimeManager")
        self.manager = self.manager_patch.start().return_value
        self.manager._port_open.return_value = False
        self.manager._verified_runtime.return_value = False
        self.addCleanup(self.manager_patch.stop)

    def write(self, path, text="test file"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def configure(self, root=None):
        root = root or self.data
        self.write(self.preferences / "runtime.json", json.dumps({"data_root": str(root)}))
        self.write(self.preferences / "settings.json", json.dumps({"model": "test:local", "log_path": "D:/Game/system/Network.log"}))

    def registered(self, root=None):
        root = root or self.data
        cleanup.register_data_root(root, self.preferences)
        return root

    def test_registration_tracks_new_root_before_runtime_exists(self):
        self.registered()
        record = json.loads((self.preferences / cleanup.REGISTRY).read_text())
        self.assertEqual(record["data_roots"], [str(self.data)])
        self.assertTrue((self.data / cleanup.MARKER).is_file())
        self.assertFalse((self.data / "ollama").exists())
        self.manager._port_open.assert_not_called()
        self.manager.stop_service.assert_not_called()

    def test_registration_is_idempotent_and_tracks_multiple_selected_roots(self):
        self.registered()
        self.registered()
        other = self.registered(self.base / "second")
        roots = json.loads((self.preferences / cleanup.REGISTRY).read_text())["data_roots"]
        self.assertEqual(roots, [str(self.data), str(other)])

    def test_unregistered_existing_managed_children_are_not_adopted(self):
        for name in cleanup.CHILDREN:
            with self.subTest(name=name):
                root = self.base / name
                self.write(root / name / "unrelated.txt")
                with self.assertRaisesRegex(SetupError, "untracked"):
                    cleanup.register_data_root(root, self.preferences)
                self.assertTrue((root / name / "unrelated.txt").is_file())
                self.assertFalse((root / cleanup.MARKER).exists())

    def test_unregistered_stage_and_archive_are_not_claimed(self):
        self.write(self.data / ".ollama-stage-abcd1234" / "file")
        with self.assertRaisesRegex(SetupError, "untracked"):
            self.registered()
        self.assertTrue((self.data / ".ollama-stage-abcd1234" / "file").exists())

    def test_registered_failed_download_root_is_removed_without_runtime(self):
        self.registered()
        archive = f"ollama-windows-amd64-{cleanup.OLLAMA_VERSION}.zip"
        self.write(self.data / (archive + ".part"))
        self.write(self.data / ".ollama-stage-abcd1234" / "partial.dll")
        self.write(self.preferences / "settings-1234abcd.tmp")
        report = cleanup.uninstall_data(self.preferences)
        self.assertTrue(report["ok"])
        self.assertFalse(self.data.exists())
        self.assertFalse(self.preferences.exists())
        self.manager.stop_service.assert_not_called()

    def test_complete_uninstall_removes_all_tracked_stores_and_preferences(self):
        roots = [self.registered(), self.registered(self.base / "other data")]
        self.configure(roots[-1])
        for root in roots:
            for child in cleanup.CHILDREN:
                self.write(root / child / "nested" / "owned.dat")
            self.write(root / f"ollama-windows-amd64-{cleanup.OLLAMA_VERSION}.zip")
        report = cleanup.uninstall_data(self.preferences)
        self.assertTrue(report["ok"])
        for root in roots:
            self.assertFalse(root.exists())
        self.assertFalse(self.preferences.exists())

    def test_unrelated_siblings_and_game_files_are_preserved(self):
        self.write(self.data / "personal-notes.txt", "keep this")
        self.registered()
        self.write(self.data / "models" / "model.bin")
        self.write(self.data / ".ollama-stage-user-folder" / "keep.txt")
        self.write(self.preferences / "personal-notes.txt", "keep prefs sibling")
        game = self.base / "Game" / "system" / "Network.log"
        self.write(game, "game chat")
        self.configure()
        cleanup.uninstall_data(self.preferences)
        self.assertEqual((self.data / "personal-notes.txt").read_text(), "keep this")
        self.assertTrue((self.data / ".ollama-stage-user-folder" / "keep.txt").exists())
        self.assertTrue((self.preferences / "personal-notes.txt").exists())
        self.assertEqual(game.read_text(), "game chat")
        self.assertFalse((self.data / "models").exists())

    def test_current_legacy_store_is_adopted_only_with_verified_runtime(self):
        self.configure()
        self.write(self.data / "models" / "model.bin")
        with self.assertRaisesRegex(SetupError, "untracked"):
            cleanup.uninstall_data(self.preferences)
        self.assertTrue((self.data / "models" / "model.bin").exists())
        self.manager._verified_runtime.return_value = True
        cleanup.uninstall_data(self.preferences)
        self.assertFalse(self.data.exists())
        self.assertFalse(self.preferences.exists())

    def test_uninstall_can_repeat_after_success(self):
        self.registered()
        cleanup.uninstall_data(self.preferences)
        self.assertTrue(cleanup.uninstall_data(self.preferences)["ok"])

    def test_missing_saved_data_root_does_not_block_preference_removal(self):
        self.configure()
        cleanup.uninstall_data(self.preferences)
        self.assertFalse(self.preferences.exists())
        self.assertFalse(self.data.exists())

    def test_unsafe_roots_and_preferences_are_rejected(self):
        roots = [Path(self.base.anchor), Path.home(), self.preferences, self.preferences.parent,
                 self.preferences / "nested", Path("relative")]
        for root in roots:
            with self.subTest(root=root), self.assertRaises(SetupError):
                cleanup.register_data_root(root, self.preferences)
        with self.assertRaises(SetupError):
            cleanup.register_data_root(self.data, self.base)

    def test_game_folder_cannot_be_registered_as_model_data(self):
        self.write(self.data / "system" / "L2.ini")
        with self.assertRaisesRegex(SetupError, "game files"):
            self.registered()
        self.assertTrue((self.data / "system" / "L2.ini").exists())

    def test_system_and_installed_application_locations_are_rejected(self):
        system = self.base / "Windows"
        with patch.dict(os.environ, {"SystemRoot": str(system)}):
            with self.assertRaises(SetupError):
                cleanup.register_data_root(system / "data", self.preferences)
        with patch("sys.frozen", True, create=True), patch("sys.executable", str(self.base / "App" / "translator.exe")):
            with self.assertRaises(SetupError):
                cleanup.register_data_root(self.base / "App" / "data", self.preferences)

    def test_ownership_marker_cannot_redirect_cleanup_elsewhere(self):
        self.registered()
        self.write(self.data / "models" / "keep.bin")
        marker = self.data / cleanup.MARKER
        value = json.loads(marker.read_text())
        value["data_root"] = str(self.base / "elsewhere")
        marker.write_text(json.dumps(value))
        with self.assertRaisesRegex(SetupError, "ownership record"):
            cleanup.uninstall_data(self.preferences)
        self.assertTrue((self.data / "models" / "keep.bin").exists())
        self.manager._port_open.assert_not_called()

    def test_registry_or_marker_corruption_blocks_cleanup(self):
        self.registered()
        for path in (self.preferences / cleanup.REGISTRY, self.data / cleanup.MARKER):
            original = path.read_text()
            path.write_text("{}")
            with self.subTest(path=path), self.assertRaises(SetupError):
                cleanup.uninstall_data(self.preferences)
            path.write_text(original)

    def test_all_roots_are_preflighted_before_any_deletion(self):
        self.registered()
        other = self.registered(self.base / "other data")
        self.write(self.data / "models" / "keep.bin")
        (other / cleanup.MARKER).write_text("{corrupt")
        with self.assertRaises(SetupError), patch.object(cleanup.shutil, "rmtree") as remove:
            cleanup.uninstall_data(self.preferences)
        remove.assert_not_called()
        self.assertTrue((self.data / "models" / "keep.bin").exists())

    def test_reparse_descendant_blocks_deletion_before_service_changes(self):
        self.registered()
        target = self.data / "models" / "redirected"
        target.mkdir(parents=True)
        real_lstat = Path.lstat
        def lstat(path, *args, **kwargs):
            value = real_lstat(path, *args, **kwargs)
            if path == target:
                return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
            return value
        with patch.object(Path, "lstat", lstat), self.assertRaisesRegex(SetupError, "linked or redirected"):
            cleanup.uninstall_data(self.preferences)
        self.assertTrue(target.exists())
        self.manager._port_open.assert_not_called()

    def test_reparse_ancestor_is_rejected_before_resolve_hides_it(self):
        parent = self.data.parent
        real_lstat = Path.lstat
        def lstat(path, *args, **kwargs):
            value = real_lstat(path, *args, **kwargs)
            if path == parent:
                return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
            return value
        with patch.object(Path, "lstat", lstat), self.assertRaisesRegex(SetupError, "linked or redirected"):
            self.registered()
        self.assertFalse(self.data.exists())

    def test_owned_service_is_stopped_before_removing_any_payload(self):
        self.registered()
        self.write(self.data / "models" / "model.bin")
        self.manager._port_open.return_value = True
        self.manager._verify_owner.return_value = {"pid": 123}
        self.manager.stop_service.side_effect = lambda: self.assertTrue((self.data / "models" / "model.bin").exists())
        report = cleanup.uninstall_data(self.preferences)
        self.manager.stop_service.assert_called_once()
        self.assertTrue(report["service_stopped"])

    def test_other_service_is_left_running(self):
        self.registered()
        self.manager._port_open.return_value = True
        # Completely different display wording must not affect the decision.
        self.manager._verify_owner.side_effect = ServiceOwnershipMismatch("Another installation owns this listener.")
        report = cleanup.uninstall_data(self.preferences)
        self.assertFalse(report["service_stopped"])
        self.manager.stop_service.assert_not_called()

    def test_generic_error_with_old_mismatch_wording_blocks_deletion(self):
        self.registered()
        payload = self.data / "models" / "model.bin"
        self.write(payload)
        self.manager._port_open.return_value = True
        failure = SetupError("Port 11435 is occupied by a different service or storage installation.")
        self.manager._verify_owner.side_effect = failure
        with self.assertRaises(SetupError) as caught:
            cleanup.uninstall_data(self.preferences)
        self.assertIs(caught.exception.__cause__, failure)
        self.assertTrue(payload.exists())
        self.assertTrue((self.preferences / cleanup.REGISTRY).exists())
        self.manager.stop_service.assert_not_called()

    def test_all_registered_roots_mismatch_without_stopping_service(self):
        roots = [self.registered(), self.registered(self.base / "other model data")]
        managers = {root: Mock() for root in roots}
        for root, manager in managers.items():
            self.write(root / "models" / "model.bin")
            manager._port_open.return_value = True
            manager._verify_owner.side_effect = ServiceOwnershipMismatch("Different runtime.")
        with patch.object(cleanup, "RuntimeManager", side_effect=managers.__getitem__):
            report = cleanup.uninstall_data(self.preferences)
        self.assertFalse(report["service_stopped"])
        for root, manager in managers.items():
            manager.stop_service.assert_not_called()
            self.assertFalse((root / "models" / "model.bin").exists())

    def test_uncertainty_among_multiple_roots_blocks_deletion_in_either_order(self):
        roots = [self.registered(), self.registered(self.base / "other model data")]
        for root in roots:
            self.write(root / "models" / "model.bin")
        for unknown_index in (0, 1):
            with self.subTest(unknown_index=unknown_index):
                managers = {root: Mock() for root in roots}
                for index, (root, manager) in enumerate(managers.items()):
                    manager._port_open.return_value = True
                    manager._verify_owner.side_effect = (SetupError("Owner unavailable.") if index == unknown_index
                                                         else ServiceOwnershipMismatch("Different runtime."))
                with patch.object(cleanup, "RuntimeManager", side_effect=managers.__getitem__), self.assertRaises(SetupError):
                    cleanup.uninstall_data(self.preferences)
                for root, manager in managers.items():
                    manager.stop_service.assert_not_called()
                    self.assertTrue((root / "models" / "model.bin").exists())

    def test_later_registered_owner_is_the_only_service_stopped(self):
        roots = [self.registered(), self.registered(self.base / "other model data")]
        managers = {root: Mock() for root in roots}
        for root, manager in managers.items():
            self.write(root / "models" / "model.bin")
            manager._port_open.return_value = True
        first, second = (managers[root] for root in roots)
        first._verify_owner.side_effect = ServiceOwnershipMismatch("Different runtime.")
        second._verify_owner.return_value = {"pid": 123}
        second.stop_service.side_effect = lambda: self.assertTrue(all((root / "models" / "model.bin").exists() for root in roots))
        with patch.object(cleanup, "RuntimeManager", side_effect=managers.__getitem__):
            report = cleanup.uninstall_data(self.preferences)
        self.assertTrue(report["service_stopped"])
        first.stop_service.assert_not_called()
        second.stop_service.assert_called_once()

    def test_mismatch_during_stop_still_blocks_deletion(self):
        self.registered()
        payload = self.data / "models" / "model.bin"
        self.write(payload)
        self.manager._port_open.return_value = True
        self.manager.stop_service.side_effect = ServiceOwnershipMismatch("The listener changed during shutdown.")
        with self.assertRaises(ServiceOwnershipMismatch):
            cleanup.uninstall_data(self.preferences)
        self.assertTrue(payload.exists())

    def test_unverified_or_unstoppable_service_blocks_data_deletion(self):
        self.registered()
        self.write(self.data / "models" / "model.bin")
        self.manager._port_open.return_value = True
        self.manager._verify_owner.side_effect = SetupError("Windows cannot identify owner")
        with self.assertRaises(SetupError):
            cleanup.uninstall_data(self.preferences)
        self.assertTrue((self.data / "models" / "model.bin").exists())
        self.manager._verify_owner.side_effect = None
        self.manager.stop_service.side_effect = SetupError("stop failed")
        with self.assertRaisesRegex(SetupError, "stop failed"):
            cleanup.uninstall_data(self.preferences)
        self.assertTrue((self.data / "models" / "model.bin").exists())

    def test_interrupted_payload_deletion_retains_records_and_can_retry(self):
        self.registered()
        for child in cleanup.CHILDREN:
            self.write(self.data / child / "payload")
        remove = shutil.rmtree
        def failure(path, *args, **kwargs):
            if Path(path).name == "models":
                raise PermissionError("fixture file is busy")
            return remove(path, *args, **kwargs)
        with patch.object(cleanup.shutil, "rmtree", failure), self.assertRaisesRegex(SetupError, "retry"):
            cleanup.uninstall_data(self.preferences)
        self.assertTrue((self.preferences / cleanup.REGISTRY).exists())
        self.assertTrue((self.data / cleanup.MARKER).exists())
        cleanup.uninstall_data(self.preferences)
        self.assertFalse(self.data.exists())

    def test_retry_after_marker_removal_preserves_unrelated_files(self):
        self.registered()
        self.write(self.data / "personal.txt")
        (self.data / cleanup.MARKER).unlink()
        cleanup.uninstall_data(self.preferences)
        self.assertTrue((self.data / "personal.txt").exists())
        self.assertFalse(self.preferences.exists())

    def test_missing_marker_with_remaining_payload_is_not_deleted(self):
        self.registered()
        self.write(self.data / "models" / "data")
        (self.data / cleanup.MARKER).unlink()
        with self.assertRaisesRegex(SetupError, "no ownership record"):
            cleanup.uninstall_data(self.preferences)
        self.assertTrue((self.data / "models" / "data").exists())

    def test_nested_data_roots_are_rejected_before_creating_child_marker(self):
        self.registered()
        nested = self.data / "models" / "nested"
        with self.assertRaisesRegex(SetupError, "overlap"):
            self.registered(nested)
        self.assertFalse(nested.exists())
        with self.assertRaises(SetupError):
            self.registered(self.data.parent)

    def test_overlap_in_saved_registry_blocks_cleanup(self):
        self.registered()
        self.write(self.data / "models" / "model.bin")
        registry = self.preferences / cleanup.REGISTRY
        record = json.loads(registry.read_text())
        record["data_roots"].append(str(self.data / "models" / "nested"))
        registry.write_text(json.dumps(record))
        with self.assertRaisesRegex(SetupError, "overlap"):
            cleanup.uninstall_data(self.preferences)
        self.assertTrue((self.data / "models" / "model.bin").exists())

    def test_current_and_legacy_preferences_are_preflighted_and_cleaned_together(self):
        legacy = self.preferences.parent / "L2Prominence" / "ChatTranslator"
        self.registered()
        old_root = self.base / "old data"
        cleanup.register_data_root(old_root, legacy)
        self.write(old_root / "models" / "old-model")
        self.write(self.data / "models" / "new-model")
        self.write(legacy / "settings.json", "{}")
        report = cleanup.uninstall_data(self.preferences)
        self.assertEqual(set(report["preference_folders"]), {str(self.preferences), str(legacy)})
        for path in (old_root, self.data, legacy, self.preferences):
            self.assertFalse(path.exists())

    def test_shared_root_across_renamed_preference_folders_is_deleted_once(self):
        legacy = self.preferences.parent / "L2Prominence" / "ChatTranslator"
        cleanup.register_data_root(self.data, legacy)
        self.registered()
        self.write(self.data / "models" / "model")
        report = cleanup.uninstall_data(self.preferences)
        self.assertEqual(report["data_roots"], [str(self.data)])
        self.assertFalse(self.data.exists())
        self.assertFalse(legacy.exists())
        self.assertFalse(self.preferences.exists())

    def test_invalid_legacy_registry_stops_current_data_deletion(self):
        legacy = self.preferences.parent / "L2Prominence" / "ChatTranslator"
        self.registered()
        self.write(self.data / "models" / "keep")
        self.write(legacy / cleanup.REGISTRY, "{bad")
        with self.assertRaises(SetupError):
            cleanup.uninstall_data(self.preferences)
        self.assertTrue((self.data / "models" / "keep").exists())

    def test_registry_save_failure_rolls_back_new_marker_and_empty_root(self):
        writer = cleanup._write_json
        def failure(path, value):
            if path.name == cleanup.REGISTRY:
                raise PermissionError("registry is not writable")
            return writer(path, value)
        with patch.object(cleanup, "_write_json", failure), self.assertRaisesRegex(SetupError, "No downloads"):
            self.registered()
        self.assertFalse(self.data.exists())
        self.assertFalse((self.preferences / cleanup.REGISTRY).exists())

    def test_registry_save_failure_retains_existing_marker_and_data(self):
        self.registered()
        self.write(self.data / "models" / "keep")
        marker = (self.data / cleanup.MARKER).read_bytes()
        writer = cleanup._write_json
        def failure(path, value):
            if path.name == cleanup.REGISTRY:
                raise PermissionError("registry is not writable")
            return writer(path, value)
        with patch.object(cleanup, "_write_json", failure), self.assertRaises(SetupError):
            self.registered()
        self.assertEqual((self.data / cleanup.MARKER).read_bytes(), marker)
        self.assertTrue((self.data / "models" / "keep").exists())


if __name__ == "__main__":
    unittest.main()
