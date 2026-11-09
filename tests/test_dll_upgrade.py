"""Upgrade verifier orchestration with synthetic binaries; no app execution."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("l2_dll_upgrade", ROOT / "packaging/verify_dll_upgrade.py")
upgrade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upgrade)


class DllUpgradeTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".build").mkdir(exist_ok=True)
        self.folder = self.enterContext(tempfile.TemporaryDirectory(prefix="dll-upgrade-test-", dir=ROOT / ".build"))
        self.root = Path(self.folder).resolve()
        self.old = self.root / "old"
        self.current = self.root / "current"
        self.work = self.root / "work"
        self.version = "1.7.4"
        self.env = {"PATH": "synthetic-clean-runtime-path"}
        self.plan_name = "_internal/assets/" + upgrade.CLEANUP_PLAN_NAME
        self.core = [f"_internal/required-{index:02}.dll" for index in range(9)]
        self.retired = [f"_internal/api-ms-win-test-{index:02}.dll" for index in range(42)] + ["_internal/ucrtbase.dll"]
        for name in ["Lineage2ChatTranslator.exe", *self.core]:
            self.write(self.old, name, ("old bytes: " + name).encode())
            self.write(self.current, name, ("current bytes: " + name).encode())
        old_hashes = []
        for name in self.retired:
            contents = ("retired bytes: " + name).encode()
            self.write(self.old, name, contents)
            old_hashes.append({"path": name, "hashes": [hashlib.sha256(contents).hexdigest()]})
        self.write(self.old, self.plan_name, b"historical inventory replaced by the new package")
        self.plan = {"schema_version": 1, "product": "Lineage 2 Chat Translator", "version": self.version,
                     "current_paths": ["Lineage2ChatTranslator.exe", *self.core, self.plan_name],
                     "obsolete_files": old_hashes}
        self.save_plan()

    def write(self, root, relative, content):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def save_plan(self):
        self.write(self.current, self.plan_name, json.dumps(self.plan).encode())

    def fake_self_test(self, app, output, version, env):
        self.assertTrue(app.is_relative_to(self.work))
        self.assertEqual(env, self.env)
        self.assertEqual(version, self.version)
        self.assertEqual(len(list((app / "_internal").glob("*.dll"))), 9)
        report = {"ok": True, "frozen": True, "version": version,
                  "windows_opened": False, "network_requests": False}
        output.write_text(json.dumps(report), encoding="utf-8")
        return report

    def run_upgrade(self):
        return upgrade.verify_upgrade(self.old, self.current, self.work, self.version, child_env=self.env)

    def test_real_cleanup_on_synthetic_payload_removes_43_and_preserves_sentinels(self):
        old_before = upgrade._inventory(self.old)
        current_before = upgrade._inventory(self.current)
        with patch.object(upgrade, "_run_self_test", side_effect=self.fake_self_test) as probe:
            report = self.run_upgrade()
        self.assertEqual(report["retired_dll_count"], 43)
        self.assertEqual(report["old_dll_count"], 52)
        self.assertEqual(report["current_dll_count"], 9)
        self.assertTrue(report["exact_current_payload_after_cleanup"])
        self.assertTrue(report["cleanup_idempotent"])
        self.assertTrue(report["modified_retired_dll_preserved"])
        self.assertFalse(report["real_installation_touched"])
        self.assertIn("settings.json", report["synthetic_protected_files_preserved"])
        self.assertIn("models/fixture-model.bin", report["synthetic_protected_files_preserved"])
        self.assertIn("game/system/Engine.dll", report["synthetic_protected_files_preserved"])
        probe.assert_called_once()
        self.assertEqual(upgrade._inventory(self.old), old_before)
        self.assertEqual(upgrade._inventory(self.current), current_before)
        self.assertEqual(list(self.work.iterdir()), [])

    def test_missing_retirement_or_unknown_hash_blocks_before_copies_and_execution(self):
        for mutation in ("missing", "hash"):
            with self.subTest(mutation=mutation):
                original = json.loads(json.dumps(self.plan))
                if mutation == "missing":
                    self.plan["obsolete_files"].pop()
                else:
                    self.plan["obsolete_files"][0]["hashes"] = ["0" * 64]
                self.save_plan()
                with patch.object(upgrade, "_run_self_test") as probe, self.assertRaises(ValueError):
                    self.run_upgrade()
                probe.assert_not_called()
                self.assertFalse(self.work.exists())
                self.plan = original
        self.save_plan()

    def test_payload_inventory_mismatch_is_not_silently_accepted(self):
        self.write(self.current, "extra-report.txt", b"must fail")
        with patch.object(upgrade, "_run_self_test") as probe, self.assertRaisesRegex(ValueError, "inventory"):
            self.run_upgrade()
        probe.assert_not_called()

    def test_incomplete_dll_reduction_is_rejected(self):
        retained = self.retired[0]
        self.write(self.current, retained, (self.old / retained).read_bytes())
        self.plan["current_paths"].append(retained)
        self.plan["obsolete_files"] = [row for row in self.plan["obsolete_files"] if row["path"] != retained]
        self.save_plan()
        with patch.object(upgrade, "_run_self_test") as probe, self.assertRaisesRegex(ValueError, "52-to-9"):
            self.run_upgrade()
        probe.assert_not_called()

    def test_failed_frozen_self_test_cleans_only_disposable_fixture(self):
        old_before = upgrade._inventory(self.old)
        current_before = upgrade._inventory(self.current)
        with patch.object(upgrade, "_run_self_test", side_effect=ValueError("synthetic probe failure")), \
             self.assertRaisesRegex(ValueError, "probe failure"):
            self.run_upgrade()
        self.assertEqual(upgrade._inventory(self.old), old_before)
        self.assertEqual(upgrade._inventory(self.current), current_before)
        self.assertEqual(list(self.work.iterdir()), [])

    def test_real_install_and_unbounded_cleanup_targets_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "child folders"):
            upgrade.verify_upgrade(ROOT, self.current, self.work, self.version, child_env=self.env)
        with self.assertRaisesRegex(ValueError, "child folders"):
            upgrade._remove_fixture(ROOT / ".build", self.work)
        with self.assertRaisesRegex(ValueError, "cleanup target"):
            upgrade._remove_fixture(self.current, self.root)
        self.assertTrue((self.current / "Lineage2ChatTranslator.exe").is_file())

    def test_frozen_probe_uses_only_headless_mode_clean_environment_and_timeout(self):
        output = self.root / "probe.json"
        def run(arguments, **kwargs):
            self.assertEqual(arguments, [str(self.current / "Lineage2ChatTranslator.exe"),
                                         "--self-test", "--output", str(output)])
            self.assertEqual(kwargs["env"], self.env)
            self.assertEqual(kwargs["timeout"], upgrade.SELF_TEST_TIMEOUT_SECONDS)
            self.assertEqual(kwargs["creationflags"], getattr(upgrade.subprocess, "CREATE_NO_WINDOW", 0))
            output.write_text(json.dumps({"ok": True, "frozen": True, "version": self.version,
                                          "windows_opened": False, "network_requests": False,
                                          "settings_path": "private fixture marker"}), encoding="utf-8")
        with patch.object(upgrade.subprocess, "run", side_effect=run):
            report = upgrade._run_self_test(self.current, output, self.version, self.env)
        self.assertNotIn("settings_path", report)


if __name__ == "__main__":
    unittest.main()
