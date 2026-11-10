"""Incremental publication artifacts use synthetic payloads, never installed apps."""
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    "l2_test_incremental_package", Path(__file__).resolve().parents[1] / "packaging/incremental_package.py")
package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package)


class IncrementalPackageTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.app = self.root / "app"
        self.release = self.root / "release"
        self.records = []
        for name, content in {"Lineage2ChatTranslator.exe": b"MZ synthetic executable",
                              "_internal/library.dll": b"synthetic library" * 1000,
                              "_internal/empty.txt": b""}.items():
            path = self.app / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            self.records.append({"path": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()})
        self.cleanup = {"schema_version": 1, "product": "Lineage 2 Chat Translator", "version": "1.2.3",
                        "current_paths": [row["path"] for row in self.records], "obsolete_files": []}

    def create(self):
        return package.create_package(self.app, self.release, "1.2.3", self.records, self.cleanup)

    def test_deterministic_objects_and_complete_inventory_match_installer_payload(self):
        descriptor = self.create()
        document, names = package.verify_package(self.release, descriptor, "1.2.3", expected_payload=self.records)
        self.assertEqual(len(document["files"]), 3)
        self.assertEqual(len(names), 4)
        before = {name: (self.release / name).read_bytes() for name in names}
        self.assertEqual(self.create(), descriptor)
        self.assertEqual(before, {name: (self.release / name).read_bytes() for name in names})
        for row in document["files"]:
            raw = gzip.decompress(before[package.blob_path(row)])
            self.assertEqual(raw, (self.app / row["path"]).read_bytes())

    def test_changed_frozen_file_blocks_package_without_accepting_stale_hashes(self):
        (self.app / self.records[0]["path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "Payload changed"):
            self.create()
        self.assertFalse((self.release / "updates").exists())

    def test_tampered_or_missing_compressed_object_is_rejected(self):
        descriptor = self.create()
        document, _ = package.verify_package(self.release, descriptor, "1.2.3")
        blob = self.release / package.blob_path(document["files"][0])
        blob.write_bytes(b"tampered gzip")
        with self.assertRaisesRegex(ValueError, "checksum/size"):
            package.verify_package(self.release, descriptor, "1.2.3")
        with self.assertRaisesRegex(ValueError, "Immutable update artifact differs"):
            self.create()
        blob.unlink()
        with self.assertRaisesRegex(ValueError, "Missing"):
            package.verify_package(self.release, descriptor, "1.2.3")

    def test_manifest_size_hash_payload_and_wire_path_are_bound(self):
        descriptor = self.create()
        with self.assertRaisesRegex(ValueError, "verified installer payload"):
            package.verify_package(self.release, descriptor, "1.2.3", expected_payload=[])
        changed = json.loads(json.dumps(descriptor))
        changed["manifest"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "manifest checksum"):
            package.verify_package(self.release, changed, "1.2.3")
        changed["manifest"]["file"] = "../settings.json"
        with self.assertRaises(ValueError):
            package.verify_package(self.release, changed, "1.2.3")

    def test_extra_public_objects_are_not_implicitly_referenced(self):
        descriptor = self.create()
        public = {"version": "1.2.3", "incremental": descriptor}
        with self.assertRaisesRegex(ValueError, "referenced update artifact is missing"):
            package.verify_public(lambda name, limit: package.read_file(self.release, name, limit), public, names=set())


if __name__ == "__main__":
    unittest.main()
