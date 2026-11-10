"""Incremental manifests, downloads and selection use synthetic, isolated files."""
import copy
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import incremental_update as incremental


VERSION = "99.8.7"
PRODUCT = "Lineage 2 Chat Translator"


def digest(content):
    return hashlib.sha256(content).hexdigest()


def record(path, content):
    compressed = gzip.compress(content, mtime=0)
    return {"path": path, "bytes": len(content), "sha256": digest(content),
            "blob": {"bytes": len(compressed), "sha256": digest(compressed)}}


def manifest(*records):
    entries = list(records) or [record("Lineage2ChatTranslator.exe", b"MZ synthetic application"),
                                record("_internal/runtime.dll", b"synthetic runtime")]
    return {"schema_version": 1, "product": PRODUCT, "platform": "windows-x64",
            "version": VERSION, "files": entries,
            "cleanup": {"schema_version": 1, "product": PRODUCT, "version": VERSION,
                        "current_paths": [entry["path"] for entry in entries], "obsolete_files": []}}


def encode(document):
    return json.dumps(document, ensure_ascii=True).encode("utf-8")


class IncrementalManifestTests(unittest.TestCase):
    def validate(self, document):
        return incremental.validate_manifest(encode(document), VERSION)

    def descriptor(self):
        data = encode(manifest())
        return {"schema_version": 1, "manifest": {"file": f"updates/{VERSION}/files.json",
                "bytes": len(data), "sha256": digest(data)}}

    def test_valid_descriptor_uses_exact_versioned_relative_manifest(self):
        descriptor = self.descriptor()
        self.assertEqual(incremental.validate_descriptor(descriptor, VERSION), descriptor)

    def test_descriptor_rejects_external_traversal_redirect_and_other_version(self):
        for name in ("https://other.invalid/files.json", "/updates/99.8.7/files.json",
                     "updates/../files.json", "updates/99.8.6/files.json", "updates/99.8.7/files.json?x=1",
                     "updates\\99.8.7\\files.json", "updates/99.8.7//files.json"):
            with self.subTest(path=name):
                descriptor = self.descriptor()
                descriptor["manifest"]["file"] = name
                with self.assertRaises(ValueError):
                    incremental.validate_descriptor(descriptor, VERSION)

    def test_descriptor_requires_exact_fields_integer_bounds_and_hex_hash(self):
        for key, value in (("bytes", True), ("bytes", 0), ("bytes", -1),
                           ("bytes", incremental.MAX_MANIFEST_BYTES + 1),
                           ("sha256", "A" * 64), ("sha256", "a" * 63), ("sha256", None)):
            with self.subTest(key=key, value=value):
                descriptor = self.descriptor()
                descriptor["manifest"][key] = value
                with self.assertRaises(ValueError):
                    incremental.validate_descriptor(descriptor, VERSION)
        for changes in ({"schema_version": True}, {"schema_version": 2}, {"url": "extra"}):
            descriptor = self.descriptor()
            descriptor.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                incremental.validate_descriptor(descriptor, VERSION)

    def test_complete_inventory_and_historical_cleanup_are_preserved(self):
        document = manifest()
        document["cleanup"]["obsolete_files"] = [
            {"path": "_internal/retired.dll", "hashes": [digest(b"older"), digest(b"oldest")]}]
        validated = self.validate(document)
        self.assertEqual(validated["version"], VERSION)
        self.assertEqual(validated["files"], document["files"])
        self.assertEqual(set(validated["cleanup"]["obsolete_files"][0]["hashes"]),
                         set(document["cleanup"]["obsolete_files"][0]["hashes"]))

    def test_manifest_rejects_other_product_platform_version_schema_or_fields(self):
        for key, value in (("product", "Different product"), ("platform", "linux-x64"),
                           ("version", "99.8.6"), ("schema_version", True),
                           ("schema_version", 2), ("untrusted", True)):
            with self.subTest(key=key, value=value):
                document = manifest()
                document[key] = value
                with self.assertRaises(ValueError):
                    self.validate(document)
        for key in manifest():
            document = manifest()
            del document[key]
            with self.subTest(missing=key), self.assertRaises(ValueError):
                self.validate(document)

    def test_manifest_rejects_invalid_json_duplicate_keys_and_oversized_bytes(self):
        valid = encode(manifest())
        duplicate_root = valid.replace(b'{', b'{"schema_version":1,', 1)
        duplicate_nested = valid.replace(b'"blob": {', b'"blob": {"bytes": 1,', 1)
        for data in (b"[]", b"null", b"{broken", b"\xff", duplicate_root, duplicate_nested,
                     b" " * (incremental.MAX_MANIFEST_BYTES + 1)):
            with self.subTest(length=len(data)), self.assertRaises(ValueError):
                incremental.validate_manifest(data, VERSION)

    def test_manifest_requires_nonempty_bounded_file_inventory(self):
        for entries in ([], "not a list", [None], [manifest()["files"][0]] * (incremental.MAX_FILES + 1)):
            document = manifest()
            document["files"] = entries
            with self.subTest(kind=type(entries).__name__, length=len(entries)), self.assertRaises(ValueError):
                self.validate(document)

    def test_file_and_blob_sizes_hashes_and_fields_are_strict(self):
        for target in ("file", "blob"):
            cases = [("bytes", True), ("bytes", -1), ("bytes", incremental.MAX_FILE_BYTES + 1),
                     ("sha256", "g" * 64), ("sha256", "a" * 65), ("sha256", None), ("extra", 1)]
            if target == "blob":
                cases.append(("bytes", 0))
            for key, value in cases:
                document = manifest()
                selected = document["files"][1]
                if target == "blob":
                    selected = selected["blob"]
                selected[key] = value
                with self.subTest(target=target, key=key, value=value), self.assertRaises(ValueError):
                    self.validate(document)

    def test_total_expanded_and_download_bytes_are_bounded(self):
        for target in ("file", "blob"):
            entries = [record("Lineage2ChatTranslator.exe", b"app")]
            for index in range(5):
                entry = record(f"_internal/file{index}.dll", str(index).encode())
                if target == "file":
                    entry["bytes"] = incremental.MAX_FILE_BYTES
                else:
                    entry["blob"]["bytes"] = incremental.MAX_FILE_BYTES
                entries.append(entry)
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.validate(manifest(*entries))

    def test_current_file_paths_reject_windows_aliases_and_escape_syntax(self):
        invalid = ("/outside", "C:/outside", "../outside", "_internal/../outside", "_internal//file",
                   "_internal/./file", "_internal\\runtime.dll", "_internal/a:stream", "_internal/*.dll",
                   "_internal/file?.dll", "_internal/file.", "_internal/file ", "_internal/ file",
                   "_internal/NUL.txt", "_internal/CON", "_internal/COM1.dll", "_internal/LPT²",
                   "_internal/CONOUT$", "_internal/a\x00b", "_internal/a\nb", "_internal/a\x7fb")
        for path in invalid:
            document = manifest()
            document["files"][1]["path"] = path
            document["cleanup"]["current_paths"][1] = path
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.validate(document)

    def test_targets_reject_private_data_uninstallers_and_unscoped_files(self):
        for path in ("settings.json", "runtime.json", "unins000.exe", "unins000.dat", "helper.exe",
                     "models/weights.bin", "logs/chat.log", "_internal/models/weights.bin",
                     "_internal/logs/chat.log", "_internal/settings.json", "_internal/runtime.json",
                     "_internal/private.local.json", "_internal/unins000.exe", "licenses/settings.json"):
            document = manifest()
            document["files"][1]["path"] = path
            document["cleanup"]["current_paths"][1] = path
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.validate(document)

    def test_windows_case_duplicates_and_file_directory_collisions_are_rejected(self):
        for paths in (("_internal/runtime.dll", "_INTERNAL/RUNTIME.DLL"),
                      ("_internal/package", "_internal/package/runtime.dll")):
            entries = [record("Lineage2ChatTranslator.exe", b"app")]
            entries.extend(record(path, b"file") for path in paths)
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                self.validate(manifest(*entries))

    def test_cleanup_current_paths_must_equal_manifest_inventory(self):
        for operation in ("missing", "extra", "different case"):
            document = manifest()
            if operation == "missing":
                document["cleanup"]["current_paths"].pop()
            elif operation == "extra":
                document["cleanup"]["current_paths"].append("_internal/unsigned.dll")
            else:
                document["cleanup"]["current_paths"][1] = "_INTERNAL/RUNTIME.DLL"
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                self.validate(document)

    def test_cleanup_cannot_remove_current_files_ancestors_or_private_data(self):
        for path in ("_internal/runtime.dll", "_internal/runtime.dll/child", "_internal",
                     "_internal/settings.json", "_internal/models/weights.bin", "unins000.exe"):
            document = manifest()
            document["cleanup"]["obsolete_files"] = [{"path": path, "hashes": [digest(b"old")]}]
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.validate(document)

    def test_repeated_raw_hashes_cannot_disagree_on_size_or_transfer_identity(self):
        for changed in ("bytes", "blob"):
            first = record("_internal/one.txt", b"same bytes")
            second = copy.deepcopy(first)
            second["path"] = "_internal/two.txt"
            if changed == "bytes":
                second["bytes"] += 1
            else:
                second["blob"]["sha256"] = "b" * 64
            document = manifest(record("Lineage2ChatTranslator.exe", b"app"), first, second)
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.validate(document)


class IncrementalBlobTests(unittest.TestCase):
    def test_verified_gzip_recovers_exact_signed_file_bytes(self):
        content = b"synthetic file content" * 100
        entry = record("_internal/file.dll", content)
        self.assertEqual(incremental.decode_blob(gzip.compress(content, mtime=0), entry), content)

    def test_empty_file_is_valid_when_both_sizes_and_hashes_match(self):
        entry = record("_internal/empty.txt", b"")
        self.assertEqual(incremental.decode_blob(gzip.compress(b"", mtime=0), entry), b"")

    def test_compressed_hash_and_size_mismatch_are_rejected(self):
        content = b"synthetic file"
        compressed = gzip.compress(content, mtime=0)
        entry = record("_internal/file.txt", content)
        for data in (compressed[:-1], compressed + b"x", bytes([compressed[0] ^ 1]) + compressed[1:]):
            with self.subTest(length=len(data)), self.assertRaises(ValueError):
                incremental.decode_blob(data, entry)

    def test_expanded_hash_and_size_mismatch_are_rejected(self):
        content = b"synthetic file"
        compressed = gzip.compress(content, mtime=0)
        for change in ({"bytes": len(content) - 1}, {"bytes": len(content) + 1}, {"sha256": "c" * 64}):
            entry = record("_internal/file.txt", content)
            entry.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                incremental.decode_blob(compressed, entry)

    def test_authenticated_malformed_or_truncated_gzip_is_rejected(self):
        complete = gzip.compress(b"synthetic file", mtime=0)
        for data in (b"not gzip", complete[:-8], complete[:-1], complete[:10] + b"broken"):
            entry = record("_internal/file.txt", b"synthetic file")
            entry["blob"] = {"bytes": len(data), "sha256": digest(data)}
            with self.subTest(length=len(data)), self.assertRaises(ValueError):
                incremental.decode_blob(data, entry)

    def test_gzip_expansion_stops_at_signed_file_size(self):
        data = gzip.compress(b"x" * (1024 * 1024), mtime=0)
        entry = record("_internal/file.txt", b"x")
        entry["blob"] = {"bytes": len(data), "sha256": digest(data)}
        with self.assertRaises(ValueError):
            incremental.decode_blob(data, entry)

    def test_concatenated_gzip_cannot_sneak_additional_output(self):
        content = b"synthetic file"
        data = gzip.compress(content, mtime=0) + gzip.compress(b"unexpected", mtime=0)
        entry = record("_internal/file.txt", content)
        entry["blob"] = {"bytes": len(data), "sha256": digest(data)}
        with self.assertRaises(ValueError):
            incremental.decode_blob(data, entry)


class IncrementalSelectionTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.root = self.base / "installation"
        self.root.mkdir()
        self.contents = {"Lineage2ChatTranslator.exe": b"MZ synthetic target application",
                         "_internal/unchanged.dll": b"synthetic unchanged runtime",
                         "_internal/changed.txt": b"synthetic new content",
                         "_internal/missing.dll": b"synthetic missing runtime"}
        self.document = incremental.validate_manifest(
            encode(manifest(*(record(name, data) for name, data in self.contents.items()))), VERSION)

    def write(self, name, content):
        target = self.root.joinpath(*name.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def test_selects_only_changed_missing_and_corrupted_files_from_full_target(self):
        self.write("Lineage2ChatTranslator.exe", self.contents["Lineage2ChatTranslator.exe"])
        self.write("_internal/unchanged.dll", self.contents["_internal/unchanged.dll"])
        self.write("_internal/changed.txt", b"synthetic previous content")
        extra = self.write("_internal/user-notes.txt", b"unmanaged data")
        selected = incremental.changed_files(self.root, self.document)
        self.assertEqual({entry["path"] for entry in selected}, {"_internal/changed.txt", "_internal/missing.dll"})
        self.assertEqual(extra.read_bytes(), b"unmanaged data")

    def test_equal_length_corruption_is_detected_by_hash(self):
        for name, content in self.contents.items():
            self.write(name, content)
        name = "_internal/unchanged.dll"
        self.write(name, b"x" * len(self.contents[name]))
        self.assertEqual([entry["path"] for entry in incremental.changed_files(self.root, self.document)], [name])

    def test_all_matching_files_need_no_download(self):
        for name, content in self.contents.items():
            self.write(name, content)
        self.assertEqual(incremental.changed_files(self.root, self.document), [])

    def test_missing_current_version_file_is_repaired_without_version_assumptions(self):
        for name, content in self.contents.items():
            self.write(name, content)
        (self.root / "Lineage2ChatTranslator.exe").unlink()
        self.assertEqual([entry["path"] for entry in incremental.changed_files(self.root, self.document)],
                         ["Lineage2ChatTranslator.exe"])

    def test_directory_at_target_file_is_rejected_and_preserved(self):
        target = self.root / "_internal" / "unchanged.dll"
        target.mkdir(parents=True)
        marker = target / "owner.txt"
        marker.write_bytes(b"preserve this")
        with self.assertRaises(ValueError):
            incremental.changed_files(self.root, self.document)
        self.assertEqual(marker.read_bytes(), b"preserve this")

    def test_file_symlink_cannot_be_treated_as_verified_unchanged_file(self):
        outside = self.base / "outside.dll"
        outside.write_bytes(self.contents["_internal/unchanged.dll"])
        target = self.root / "_internal" / "unchanged.dll"
        target.parent.mkdir()
        try:
            target.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"File symlinks unavailable: {exc}")
        with self.assertRaises(ValueError):
            incremental.changed_files(self.root, self.document)
        self.assertEqual(outside.read_bytes(), self.contents["_internal/unchanged.dll"])

    def test_hard_link_cannot_be_treated_as_verified_unchanged_file(self):
        outside = self.base / "outside.dll"
        outside.write_bytes(self.contents["_internal/unchanged.dll"])
        target = self.root / "_internal" / "unchanged.dll"
        target.parent.mkdir()
        try:
            os.link(outside, target)
        except OSError as exc:
            self.skipTest(f"Hard links unavailable: {exc}")
        with self.assertRaises(ValueError):
            incremental.changed_files(self.root, self.document)
        self.assertEqual(outside.read_bytes(), self.contents["_internal/unchanged.dll"])

    def test_windows_reparse_attribute_is_rejected_without_following_it(self):
        target = self.write("_internal/unchanged.dll", self.contents["_internal/unchanged.dll"])
        original = Path.lstat

        def inspected(path, *args, **kwargs):
            state = original(path, *args, **kwargs)
            if path == target:
                return SimpleNamespace(st_mode=state.st_mode, st_file_attributes=0x400)
            return state

        with patch.object(Path, "lstat", inspected), self.assertRaises(ValueError):
            incremental.changed_files(self.root, self.document)

    def test_directory_symlink_cannot_redirect_missing_or_matching_files(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "unchanged.dll").write_bytes(self.contents["_internal/unchanged.dll"])
        try:
            (self.root / "_internal").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Directory symlinks unavailable: {exc}")
        with self.assertRaises(ValueError):
            incremental.changed_files(self.root, self.document)
        self.assertEqual((outside / "unchanged.dll").read_bytes(), self.contents["_internal/unchanged.dll"])

    def test_cancel_before_selection_does_not_read_installed_files(self):
        event = threading.Event()
        event.set()
        with patch.object(Path, "open", side_effect=AssertionError("canceled operation read a file")):
            with self.assertRaises(ValueError):
                incremental.changed_files(self.root, self.document, cancelled=event)


if __name__ == "__main__":
    unittest.main()
