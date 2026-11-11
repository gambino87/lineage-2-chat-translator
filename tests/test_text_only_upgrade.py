"""Protected frontend upgrades use inert files, no services or model inference."""
import hashlib
import os
import unittest
from unittest.mock import patch

import installer_runtime as runtime
import runtime_integrity as integrity
from tests import test_installer_runtime as support


@unittest.skipUnless(os.name == "nt", "Native private storage uses Windows locks")
class TextOnlyUpgradeTests(unittest.TestCase):
    setUp = support.RuntimeTests.setUp

    def predecessor_runtime(self):
        self.fixture_files["lib/ollama/native.dll"] = b"unchanged synthetic native library"
        self.predecessor = b"reviewed synthetic predecessor; never executed"
        self.enterContext(patch.object(integrity, "PREDECESSOR_FRONTENDS", {
            hashlib.sha256(self.predecessor).hexdigest(): len(self.predecessor)}))
        for name, content in self.fixture_files.items():
            target = self.manager.runtime_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.predecessor if name == "ollama.exe" else content)
        self.model = self.manager.data_root / "models/blobs/synthetic-model"
        self.model.parent.mkdir(parents=True)
        self.model.write_bytes(b"synthetic model; preserve exactly")

    def test_predecessor_upgrades_without_download_and_second_ensure_reuses_it(self):
        self.predecessor_runtime()
        native = self.manager.runtime_dir / "lib/ollama/native.dll"
        with patch.object(self.manager, "_download_runtime") as download, \
             patch.object(runtime.subprocess, "Popen") as process, \
             patch.object(self.manager, "_copy_protected_frontend",
                          wraps=self.manager._copy_protected_frontend) as copy:
            self.assertEqual(self.manager.ensure_runtime(), self.manager.executable)
            self.assertEqual(self.manager.executable.read_bytes(), self.fixture_files["ollama.exe"])
            self.assertTrue(self.manager._verified_runtime())
            self.assertEqual(self.manager.ensure_runtime(), self.manager.executable)
            copy.assert_called_once_with(self.manager.runtime_dir)
            download.assert_not_called()
            process.assert_not_called()
        self.assertEqual(native.read_bytes(), self.fixture_files["lib/ollama/native.dll"])
        self.assertEqual(self.model.read_bytes(), b"synthetic model; preserve exactly")
        self.assertFalse(list(self.manager.runtime_dir.glob(".frontend-*")))

    def test_unknown_frontend_is_preserved_with_models(self):
        self.predecessor_runtime()
        self.manager.executable.write_bytes(b"unknown frontend")
        self.assert_refused_unchanged()

    def test_tampered_native_library_is_preserved_with_predecessor_and_models(self):
        self.predecessor_runtime()
        (self.manager.runtime_dir / "lib/ollama/native.dll").write_bytes(b"tampered native library")
        self.assert_refused_unchanged()

    def test_unlisted_executable_is_preserved_with_predecessor_and_models(self):
        self.predecessor_runtime()
        (self.manager.runtime_dir / "unknown.exe").write_bytes(b"unknown executable")
        self.assert_refused_unchanged()

    def assert_refused_unchanged(self):
        before = {path.relative_to(self.manager.data_root): path.read_bytes()
                  for path in self.manager.data_root.rglob("*") if path.is_file()}
        with patch.object(self.manager, "_download_runtime") as download, \
             patch.object(runtime.subprocess, "Popen") as process, \
             patch.object(self.manager, "_copy_protected_frontend") as copy:
            with self.assertRaisesRegex(runtime.SetupError, "could not be verified"):
                self.manager.ensure_runtime()
            download.assert_not_called()
            process.assert_not_called()
            copy.assert_not_called()
        after = {path.relative_to(self.manager.data_root): path.read_bytes()
                 for path in self.manager.data_root.rglob("*") if path.is_file()}
        self.assertEqual(after, before)
