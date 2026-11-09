"""Obsolete-file cleanup uses isolated synthetic installation trees only."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import installation_cleanup as cleanup


class InstallationCleanupTests(unittest.TestCase):
    VERSION = "1.7.3"

    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.root = self.base / "installation"
        self.root.mkdir()
        self.current = self.write("_internal/current.dll", b"current runtime")
        self.write("Lineage2ChatTranslator.exe", b"current executable")

    def write(self, name, contents=b"previous release"):
        target = self.root.joinpath(*name.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
        return target

    def entry(self, name, content=b"previous release"):
        return {"path": name, "hashes": [hashlib.sha256(content).hexdigest()]}

    def plan(self, *entries):
        return {"schema_version": 1, "product": cleanup.PRODUCT, "version": self.VERSION,
                "current_paths": ["Lineage2ChatTranslator.exe", "_internal/current.dll"],
                "obsolete_files": list(entries)}

    def run_cleanup(self, plan):
        return cleanup.cleanup_program_files(self.root, plan, self.VERSION)

    def test_removes_only_matching_retired_files_and_empty_nested_directories(self):
        retired = self.write("_internal/retired/package/old.dll")
        unknown = self.write("_internal/custom.dll", b"user addition")
        self.write("licenses/OLD-LICENSE.txt")
        plan = self.plan(self.entry("_internal/retired/package/old.dll"), self.entry("licenses/OLD-LICENSE.txt"))
        result = self.run_cleanup(plan)
        self.assertEqual(result["removed"], [entry["path"] for entry in plan["obsolete_files"]])
        self.assertEqual(result["removed_directories"], ["_internal/retired/package", "_internal/retired"])
        self.assertFalse(retired.exists())
        self.assertTrue(unknown.exists())
        self.assertTrue(self.current.exists())
        self.assertTrue((self.root / "_internal").is_dir())
        self.assertTrue((self.root / "licenses").is_dir())

    def test_matches_any_known_previous_version_hash(self):
        self.write("_internal/old.dll", b"second historical release")
        entry = self.entry("_internal/old.dll")
        entry["hashes"].append(hashlib.sha256(b"second historical release").hexdigest())
        self.assertEqual(self.run_cleanup(self.plan(entry))["removed"], [entry["path"]])

    def test_modified_retired_files_and_nonempty_directories_are_preserved(self):
        changed = self.write("_internal/old/modified.txt", b"user modified")
        self.write("_internal/old/retired.dll")
        self.write("_internal/old/my-notes.txt", b"unmanaged")
        result = self.run_cleanup(self.plan(self.entry("_internal/old/modified.txt"), self.entry("_internal/old/retired.dll")))
        self.assertEqual(result["preserved"], ["_internal/old/modified.txt"])
        self.assertEqual(result["removed_directories"], [])
        self.assertEqual(changed.read_bytes(), b"user modified")

    def test_modified_retired_binaries_block_success_and_remain_owner_removable(self):
        for name in ('_internal/ucrtbase.dll', '_internal/old.PYD'):
            with self.subTest(name=name):
                path = self.write(name, b'harmless changed fixture')
                plan = self.plan(self.entry(name))
                with self.assertRaisesRegex(cleanup.CleanupError, 'manually'):
                    self.run_cleanup(plan)
                self.assertEqual(path.read_bytes(), b'harmless changed fixture')
                path.unlink()
                self.assertEqual(self.run_cleanup(plan)['removed'], [])

    def test_missing_files_and_repeated_cleanup_are_idempotent(self):
        self.write("_internal/old.dll")
        plan = self.plan(self.entry("_internal/old.dll"), self.entry("_internal/absent.dll"))
        self.assertEqual(self.run_cleanup(plan)["removed"], ["_internal/old.dll"])
        self.assertEqual(self.run_cleanup(plan)["removed"], [])

    def test_settings_models_logs_and_uninstaller_files_remain_untouched(self):
        names = ["settings.json", "runtime.json", "models/model.bin", "logs/chat.log", "unins000.dat", "unins000.exe"]
        for name in names:
            self.write(name, b"personal data")
        self.write("_internal/old.dll")
        self.run_cleanup(self.plan(self.entry("_internal/old.dll")))
        for name in names:
            self.assertEqual((self.root / name).read_bytes(), b"personal data")

    def test_rejects_current_file_or_ancestor_or_descendant_overlap_before_deletion(self):
        old = self.write("_internal/old.dll")
        for name in ["_internal/current.dll", "_INTERNAL/CURRENT.DLL", "_internal/current.dll/child", "_internal/current"]:
            plan = self.plan(self.entry("_internal/old.dll"), self.entry(name))
            if name == "_internal/current":
                plan["current_paths"].append("_internal/current/nested.txt")
            with self.subTest(path=name), self.assertRaises(cleanup.CleanupError):
                self.run_cleanup(plan)
            self.assertTrue(old.exists())

    def test_rejects_dangerous_windows_paths(self):
        names = ["/absolute", "C:/outside", "../outside", "_internal/../outside", "_internal//a", "_internal/./a",
                 "_internal\\old.dll", "_internal/a:stream", "_internal/*.dll", "_internal/a?.dll", "_internal/a. ",
                 "_internal/a.", "_internal/ old.dll", "_internal/con", "_internal/NUL.txt", "_internal/COM1.dll",
                 "_internal/LPT²", "_internal/CONOUT$", "_internal/a\x00b", "_internal/a\nb", "_internal/a\x7fb"]
        for name in names:
            with self.subTest(path=name), self.assertRaises(cleanup.CleanupError):
                cleanup.safe_relative_path(name)

    def test_rejects_private_and_executable_targets_even_inside_internal(self):
        names = ["models/model.bin", "logs/old.log", "settings.json", "_internal/models/file", "_internal/logs/file",
                 "_internal/settings.json", "_internal/runtime.json", "_internal/Network.log", "_internal/private.local.json",
                 "_internal/helper.exe", "_internal/unins000.dat", "licenses/unins-old.txt", "Lineage2ChatTranslator.exe"]
        for name in names:
            with self.subTest(path=name), self.assertRaises(cleanup.CleanupError):
                cleanup.validate_plan(self.plan(self.entry(name)), self.VERSION)

    def test_rejects_duplicate_case_paths_and_file_directory_collisions(self):
        for entries in [("_internal/a.dll", "_INTERNAL/A.DLL"), ("_internal/pkg", "_internal/pkg/a.dll")]:
            with self.subTest(entries=entries), self.assertRaises(cleanup.CleanupError):
                cleanup.validate_plan(self.plan(*(self.entry(name) for name in entries)), self.VERSION)
        plan = self.plan()
        plan["current_paths"].append("_INTERNAL/CURRENT.DLL")
        with self.assertRaises(cleanup.CleanupError):
            cleanup.validate_plan(plan, self.VERSION)

    def test_rejects_malformed_plan_before_removing_valid_earlier_entry(self):
        old = self.write("_internal/old.dll")
        bad = self.entry("_internal/bad.dll")
        bad["hashes"] = ["not a hash"]
        with self.assertRaises(cleanup.CleanupError):
            self.run_cleanup(self.plan(self.entry("_internal/old.dll"), bad))
        self.assertTrue(old.exists())

    def test_rejects_wrong_product_version_and_schema(self):
        for key, value in (("product", "Other app"), ("version", "1.0.0"), ("schema_version", True), ("schema_version", 2)):
            plan = self.plan()
            plan[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(cleanup.CleanupError):
                cleanup.validate_plan(plan, self.VERSION)

    def test_directory_at_retired_file_path_blocks_all_deletions(self):
        old = self.write("_internal/old.dll")
        (self.root / "_internal/directory.dll").mkdir()
        with self.assertRaises(cleanup.CleanupError):
            self.run_cleanup(self.plan(self.entry("_internal/old.dll"), self.entry("_internal/directory.dll")))
        self.assertTrue(old.exists())

    def test_symlink_to_external_file_is_not_followed(self):
        external = self.base / "external.dll"
        external.write_bytes(b"previous release")
        link = self.root / "_internal/link.dll"
        try:
            link.symlink_to(external)
        except OSError:
            self.skipTest("Symlink creation is unavailable for this Windows account")
        old = self.write("_internal/old.dll")
        with self.assertRaises(cleanup.CleanupError):
            self.run_cleanup(self.plan(self.entry("_internal/old.dll"), self.entry("_internal/link.dll")))
        self.assertTrue(old.exists())
        self.assertEqual(external.read_bytes(), b"previous release")

    def test_symlink_in_installation_ancestor_is_not_followed(self):
        link = self.base / "linked-root"
        try:
            link.symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest("Symlink creation is unavailable for this Windows account")
        self.write("_internal/old.dll")
        with self.assertRaises(cleanup.CleanupError):
            cleanup.cleanup_program_files(link, self.plan(self.entry("_internal/old.dll")), self.VERSION)
        self.assertTrue((self.root / "_internal/old.dll").exists())

    def test_reparse_point_attributes_are_rejected_without_following_them(self):
        old = self.write("_internal/old.dll")
        original = Path.lstat

        def fake_info(path):
            info = original(path)
            if path == self.root / "_internal":
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=cleanup.FILE_ATTRIBUTE_REPARSE_POINT)
            return info

        with patch.object(Path, "lstat", fake_info), self.assertRaises(cleanup.CleanupError):
            self.run_cleanup(self.plan(self.entry("_internal/old.dll")))
        self.assertTrue(old.exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_real_directory_junction_cannot_redirect_cleanup(self):
        import _winapi
        external = self.base / "external"
        external.mkdir()
        external_file = external / "old.dll"
        external_file.write_bytes(b"previous release")
        link = self.root / "_internal/junction"
        _winapi.CreateJunction(str(external), str(link))
        self.addCleanup(link.rmdir)  # Remove only this synthetic junction itself.
        old = self.write("_internal/old.dll")
        with self.assertRaises(cleanup.CleanupError):
            self.run_cleanup(self.plan(self.entry("_internal/old.dll"), self.entry("_internal/junction/old.dll")))
        self.assertTrue(old.exists())
        self.assertEqual(external_file.read_bytes(), b"previous release")

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_real_junction_installation_root_is_rejected(self):
        import _winapi
        link = self.base / "junction-installation"
        _winapi.CreateJunction(str(self.root), str(link))
        self.addCleanup(link.rmdir)
        old = self.write("_internal/old.dll")
        with self.assertRaises(cleanup.CleanupError):
            cleanup.cleanup_program_files(link, self.plan(self.entry("_internal/old.dll")), self.VERSION)
        self.assertTrue(old.exists())

    def test_file_swapped_during_hash_is_preserved(self):
        old = self.write("_internal/old.dll")
        original = cleanup._file_state
        calls = 0

        def changed_state(path):
            nonlocal calls
            calls += 1
            if calls == 3:
                path.write_bytes(b"replacement content")
            return original(path)

        with patch.object(cleanup, "_file_state", changed_state), self.assertRaises(cleanup.CleanupError):
            self.run_cleanup(self.plan(self.entry("_internal/old.dll")))
        self.assertEqual(old.read_bytes(), b"replacement content")

    def test_partial_failure_remains_visible_and_can_be_retried(self):
        first = self.write("_internal/first.dll")
        blocked = self.write("_internal/blocked.dll")
        plan = self.plan(self.entry("_internal/first.dll"), self.entry("_internal/blocked.dll"))
        original = Path.unlink

        def fail_blocked(path, *args, **kwargs):
            if path == blocked:
                raise PermissionError("Synthetic busy file")
            return original(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_blocked), self.assertRaises(PermissionError):
            self.run_cleanup(plan)
        self.assertFalse(first.exists())
        self.assertTrue(blocked.exists())
        self.assertEqual(self.run_cleanup(plan)["removed"], ["_internal/blocked.dll"])

    def test_oversized_modified_file_is_preserved_without_reading(self):
        old = self.write("_internal/large.dll", b"123456789")
        with patch.object(cleanup, "MAX_OBSOLETE_FILE_BYTES", 8), self.assertRaisesRegex(cleanup.CleanupError, 'manually'):
            self.run_cleanup(self.plan(self.entry("_internal/large.dll")))
        self.assertTrue(old.exists())

    def test_read_plan_is_bounded_and_rejects_duplicate_fields(self):
        path = self.write("_internal/assets/" + cleanup.CLEANUP_PLAN_NAME, json.dumps(self.plan()).encode())
        self.assertEqual(cleanup.read_cleanup_plan(path, self.VERSION), self.plan())
        with patch.object(cleanup, "MAX_PLAN_BYTES", 8), self.assertRaises(cleanup.CleanupError):
            cleanup.read_cleanup_plan(path, self.VERSION)
        path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
        with self.assertRaises(cleanup.CleanupError):
            cleanup.read_cleanup_plan(path, self.VERSION)

    def test_rejects_relative_or_filesystem_root_installation(self):
        for root in (Path("relative"), Path(self.root.anchor)):
            with self.subTest(root=str(root)), self.assertRaises(cleanup.CleanupError):
                cleanup.cleanup_program_files(root, self.plan(), self.VERSION)


if __name__ == "__main__":
    unittest.main()
