"""Real Windows loader-lock cleanup regression in isolated .build fixtures.

Only frozen self-tests are substituted for the synthetic EXE in these unit
fixtures. DLL loading, deletion denial and the shipped PowerShell cleanup all
execute for real. The release verifier runs the actual frozen self-tests.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("l2_locked_dll_cleanup", ROOT / "packaging/verify_locked_dll_cleanup.py")
locked = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(locked)
from build_environment import EXPECTED_DLLS, clean_environment, windows_directories


@unittest.skipUnless(os.name == "nt", "The loader-lock regression requires Windows.")
class LockedDllCleanupTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".build").mkdir(exist_ok=True)
        self.folder = self.enterContext(tempfile.TemporaryDirectory(prefix="locked-dll-test-", dir=ROOT / ".build"))
        self.root = locked._build_path(Path(self.folder).resolve())
        self.old, self.current, self.work = [self.root / name for name in ("old", "current", "work")]
        self.version = "1.7.5"
        windows, system = windows_directories()
        self.env = clean_environment(os.environ, windows=windows, system=system)
        self.plan_name = "_internal/assets/" + locked.CLEANUP_PLAN_NAME
        self.helper_name = "_internal/assets/cleanup-program-files.ps1"
        self.required = ["_internal/" + name for name in sorted(EXPECTED_DLLS)]
        self.retired = [f"_internal/api-ms-win-test-{index:02}.dll" for index in range(42)] + [locked.UCRT_PATH]
        for relative in ["Lineage2ChatTranslator.exe", *self.required]:
            self.write(self.old, relative, ("historical fixture " + relative).encode())
            self.write(self.current, relative, ("current fixture " + relative).encode())
        self.write(self.old, self.plan_name, b"historical inventory replaced by current package")
        for relative in self.retired:
            self.write(self.old, relative, ("historical DLL fixture " + relative).encode())
        # This is a genuine PE DLL loaded by the Windows loader, not a mocked
        # sharing violation. Only this disposable copy is touched.
        shutil.copy2(system / "ucrtbase.dll", self.old / locked.UCRT_PATH)
        self.write(self.current, self.helper_name, (ROOT / "assets/cleanup-program-files.ps1").read_bytes())
        self.plan = {
            "schema_version": 1, "product": "Lineage 2 Chat Translator", "version": self.version,
            "current_paths": ["Lineage2ChatTranslator.exe", *self.required, self.plan_name, self.helper_name],
            "obsolete_files": [{"path": relative, "hashes": [self.digest(self.old / relative)]}
                               for relative in self.retired],
        }
        self.save_plan()

    @staticmethod
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def write(root, relative, content):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def save_plan(self):
        self.write(self.current, self.plan_name, json.dumps(self.plan).encode("utf-8"))

    def run_verification(self):
        return locked.verify_locked_upgrade(self.old, self.current, self.work, self.version, child_env=self.env)

    def synthetic_self_test(self, app, output, version, child_env):
        self.assertEqual(child_env, self.env)
        self.assertEqual(version, self.version)
        self.assertEqual({path.name.casefold() for path in (app / "_internal").glob("*.dll")}, EXPECTED_DLLS)
        self.assertFalse((app / locked.UCRT_PATH).exists())
        report = {"ok": True, "frozen": True, "version": version,
                  "windows_opened": False, "network_requests": False, "synthetic_executable": True}
        output.write_text(json.dumps(report), encoding="utf-8")
        return report

    def test_real_loaded_ucrt_blocks_then_external_cleanup_removes_it_after_release(self):
        old_before, current_before = locked._inventory(self.old), locked._inventory(self.current)
        dirty_report = {"ok": False, "version": self.version,
                        "error": "An obsolete app-local Windows runtime remains; synthetic executable"}
        with patch.object(locked, "_run_self_test", side_effect=self.synthetic_self_test) as frozen, \
             patch.object(locked, "_run_dirty_self_test", return_value=dirty_report) as dirty:
            report = self.run_verification()
        self.assertTrue(report["ok"])
        self.assertEqual(report["current_dll_count"], 9)
        self.assertEqual(report["old_dll_count"], 52)
        self.assertEqual(report["retired_dll_count"], 43)
        self.assertIn(report["os_deletion_error_while_loaded"], {5, 32, 33})
        self.assertTrue(report["helper_reports_locked_failure"])
        self.assertTrue(report["holder_exited_normally"])
        self.assertIn(locked.UCRT_PATH, report["removed_after_release"])
        self.assertTrue(report["exact_current_payload_after_cleanup"])
        self.assertTrue(report["cleanup_idempotent"])
        self.assertTrue(report["modified_retired_dll_preserved"])
        self.assertFalse(report["real_installation_touched"])
        self.assertFalse(report["windows_opened"])
        self.assertIn("settings.json", report["synthetic_protected_files_preserved"])
        self.assertIn("models/fixture-model.bin", report["synthetic_protected_files_preserved"])
        self.assertIn("game/system/Engine.dll", report["synthetic_protected_files_preserved"])
        self.assertEqual(report["partial_upgrade"]["starting_retired_dlls"], [locked.UCRT_PATH])
        self.assertEqual(report["partial_upgrade"]["removed_after_release"], [locked.UCRT_PATH])
        self.assertTrue(report["partial_upgrade"]["holder_exited_normally"])
        self.assertTrue(report["partial_upgrade"]["exact_current_payload_after_cleanup"])
        self.assertEqual(frozen.call_count, 2)
        dirty.assert_called_once()
        self.assertEqual(locked._inventory(self.old), old_before)
        self.assertEqual(locked._inventory(self.current), current_before)
        self.assertEqual(list(self.work.iterdir()), [])

    def test_unrecognized_historical_bytes_stop_before_loading_or_copying(self):
        self.plan["obsolete_files"][-1]["hashes"] = ["0" * 64]
        self.save_plan()
        with patch.object(locked, "hold_loaded_dll") as holder, self.assertRaisesRegex(ValueError, "recognize"):
            self.run_verification()
        holder.assert_not_called()
        self.assertFalse(self.work.exists())

    def test_actual_install_or_unbounded_paths_are_rejected(self):
        with patch.object(locked, "hold_loaded_dll") as holder, self.assertRaisesRegex(ValueError, "child folders"):
            locked.verify_locked_upgrade(ROOT, self.current, self.work, self.version, child_env=self.env)
        holder.assert_not_called()
        self.assertTrue((self.current / "Lineage2ChatTranslator.exe").is_file())

    def test_unlocked_file_does_not_count_as_loader_lock_evidence(self):
        path = self.root / "not-loaded.dll"
        path.write_bytes(b"unloaded fixture")
        with self.assertRaisesRegex(ValueError, "allowed deletion"):
            locked.require_os_delete_lock(path)
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
