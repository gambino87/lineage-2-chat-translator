"""Synthetic runtime inventories; native locks only in disposable fixture trees."""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import runtime_integrity as integrity
import secure_storage as storage


def manifest_for(files, legacy=b"official frontend"):
    return {"version": integrity.BACKEND_VERSION, "upstream_version": "0.40.14",
            "archive_sha256": "a" * 64,
            "upstream_executable_sha256": hashlib.sha256(legacy).hexdigest(),
            "files": [{"path": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
                      for name, content in files.items()]}


class ManifestValidationTests(unittest.TestCase):
    def test_manifest_rejects_traversal_duplicate_and_wrong_code_identity(self):
        cases = []
        valid = manifest_for({"ollama.exe": b"protected"})
        cases.append({**valid, "version": "untrusted"})
        for name in ("../other.exe", "/other.exe", "x/../other.exe", "x\\other.exe", "x:stream"):
            value = json.loads(json.dumps(valid))
            value["files"].append({"path": name, "bytes": 1, "sha256": "0" * 64})
            cases.append(value)
        value = json.loads(json.dumps(valid))
        value["files"].append({**value["files"][0], "path": "OLLAMA.EXE"})
        cases.append(value)
        with tempfile.TemporaryDirectory(prefix="l2ct-manifest-") as temporary:
            path = Path(temporary) / "manifest.json"
            for value in cases:
                with self.subTest(value=value):
                    path.write_text(json.dumps(value))
                    with patch.object(integrity, "MANIFEST_PATH", path), self.assertRaises(storage.StorageError):
                        integrity.load_manifest()
            path.write_text(json.dumps(valid))
            with patch.object(integrity, "MANIFEST_PATH", path):
                self.assertEqual(integrity.load_manifest(), valid)


@unittest.skipUnless(os.name == "nt", "Native Windows file locks are required")
class RuntimeIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="l2ct-runtime-integrity-")
        self.addCleanup(self.temp.cleanup)
        self.boundary = Path(self.temp.name) / "owned-fixture"
        self.boundary.mkdir()
        api = storage._Windows()
        handle = api.open(self.boundary, repair=True)
        try:
            api.protect(handle, True)
        finally:
            api.k.CloseHandle(handle)
        self.ancestors = patch.object(storage, "_ancestor_paths", lambda path:
                                     [parent for parent in reversed(path.parents)
                                      if parent == self.boundary or parent.is_relative_to(self.boundary)])
        self.ancestors.start()
        self.addCleanup(self.ancestors.stop)
        self.root = self.boundary / "app"
        with storage.guard_private_tree(self.root, create=True):
            self.runtime = self.root / "ollama"
            self.runtime.mkdir()
            self.files = {"ollama.exe": b"protected authenticated frontend", "cuda/gpu.dll": b"vendor GPU library",
                          "licenses/runtime.txt": b"vendor license"}
            for name, content in self.files.items():
                path = self.runtime / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        self.manifest = manifest_for(self.files)
        self.loader = patch.object(integrity, "load_manifest", return_value=self.manifest)
        self.loader.start()
        self.addCleanup(self.loader.stop)

    def verified(self, **options):
        with storage.guard_private_tree(self.root):
            return integrity.verified_runtime(self.runtime, **options)

    def test_protected_inventory_verifies_without_any_mutable_receipt(self):
        self.assertTrue(self.verified())
        self.assertFalse((self.runtime / "verified-install.json").exists())

    def test_forged_receipt_cannot_authorize_changed_executable(self):
        target = self.runtime / "ollama.exe"
        target.write_bytes(b"substituted executable")
        (self.runtime / "verified-install.json").write_text(json.dumps({
            "version": integrity.BACKEND_VERSION, "archiveSha256": self.manifest["archive_sha256"],
            "executableSha256": hashlib.sha256(target.read_bytes()).hexdigest()}))
        self.assertFalse(self.verified())
        self.assertEqual(target.read_bytes(), b"substituted executable")

    def test_dll_tampering_and_missing_members_fail_without_removing_files(self):
        for name in ("cuda/gpu.dll", "ollama.exe", "licenses/runtime.txt"):
            target = self.runtime / name
            original = target.read_bytes()
            with self.subTest(name=name, mutation="changed"):
                target.write_bytes(original + b"tampered")
                self.assertFalse(self.verified())
                self.assertTrue(target.exists())
            target.write_bytes(original)
            with self.subTest(name=name, mutation="missing"):
                target.unlink()
                self.assertFalse(self.verified())
            target.write_bytes(original)

    def test_unexpected_loadable_file_fails_without_deleting_owner_files(self):
        for name in ("injected.DLL", "helper.exe", "cuda/extension.pyd", "extra.com"):
            target = self.runtime / name
            target.write_bytes(b"unknown owner file")
            with self.subTest(name=name):
                self.assertFalse(self.verified())
                self.assertEqual(target.read_bytes(), b"unknown owner file")
            target.unlink()
        self.assertTrue(self.verified())

    def test_hardlinked_expected_file_is_rejected(self):
        target = self.runtime / "cuda" / "gpu.dll"
        outside = self.boundary / "outside.dll"
        outside.write_bytes(target.read_bytes())
        target.unlink()
        os.link(outside, target)
        with self.assertRaisesRegex(storage.StorageError, "Hard-linked"):
            integrity.pin_runtime(self.runtime)
        self.assertEqual(outside.read_bytes(), self.files["cuda/gpu.dll"])

    def test_junction_with_matching_outside_library_is_rejected(self):
        import _winapi
        directory = self.runtime / "cuda"
        original = self.runtime / "original-cuda"
        directory.rename(original)
        outside = self.boundary / "outside"
        outside.mkdir()
        (outside / "gpu.dll").write_bytes(self.files["cuda/gpu.dll"])
        _winapi.CreateJunction(str(outside), str(directory))
        try:
            with self.assertRaisesRegex(storage.StorageError, "linked|redirected"):
                integrity.pin_runtime(self.runtime)
            self.assertEqual((outside / "gpu.dll").read_bytes(), self.files["cuda/gpu.dll"])
        finally:
            directory.rmdir()

    def test_pins_block_code_write_and_delete_then_restore_owner_control(self):
        with storage.guard_private_tree(self.root):
            pins = integrity.pin_runtime(self.runtime)
            try:
                for name in self.files:
                    target = self.runtime / name
                    with self.subTest(name=name):
                        self.assertEqual(target.read_bytes(), self.files[name])
                        with self.assertRaises(OSError):
                            target.write_bytes(b"changed")
                        with self.assertRaises(OSError):
                            target.unlink()
            finally:
                pins.close()
            for name in self.files:
                target = self.runtime / name
                target.write_bytes(b"owner edit")
                self.assertEqual(target.read_bytes(), b"owner edit")
                target.unlink()

    def test_failed_later_hash_releases_earlier_file_pins(self):
        target = self.runtime / "cuda" / "gpu.dll"
        target.write_bytes(b"mismatch after frontend was pinned")
        with storage.guard_private_tree(self.root):
            with self.assertRaises(storage.StorageError):
                integrity.pin_runtime(self.runtime)
            (self.runtime / "ollama.exe").write_bytes(b"owner edit after failure")

    def test_cancellation_releases_previously_opened_files(self):
        calls = 0
        def check():
            nonlocal calls
            calls += 1
            if calls == 9:
                raise RuntimeError("synthetic cancellation")
        with storage.guard_private_tree(self.root):
            with self.assertRaisesRegex(RuntimeError, "synthetic cancellation"):
                integrity.pin_runtime(self.runtime, check=check)
            (self.runtime / "ollama.exe").write_bytes(b"owner edit after cancellation")

    def test_legacy_vendor_frontend_is_distinct_from_authenticated_frontend(self):
        self.assertTrue(self.verified())
        self.assertFalse(self.verified(legacy=True))
        executable = self.runtime / "ollama.exe"
        executable.write_bytes(b"official frontend")
        self.assertTrue(self.verified(legacy=True))
        self.assertFalse(self.verified())
        executable.write_bytes(self.files["ollama.exe"])
        self.assertTrue(self.verified())
        self.assertFalse(self.verified(legacy=True))


if __name__ == "__main__":
    unittest.main()
