"""Current-only installer payloads and retired-file provenance, using fixtures."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


_SPEC = importlib.util.spec_from_file_location(
    "l2_payload_layout", Path(__file__).resolve().parents[1] / "packaging/payload_layout.py"
)
layout = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(layout)


def record(name, contents=b"current synthetic payload"):
    return {"path": name, "bytes": len(contents), "sha256": hashlib.sha256(contents).hexdigest()}


class PayloadLayoutTests(unittest.TestCase):
    ASSETS = ("assets/L2CT.ico", "assets/item_catalog.json")

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.app = self.root / "current-app"
        self.app.mkdir()
        self.releases = self.root / "releases"
        self.releases.mkdir()
        self.current = [record("Lineage2ChatTranslator.exe"), record("_internal/current.dll")]

    def write(self, relative, contents=b"synthetic payload"):
        path = self.app / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        return path

    def history(self, version, rows, *, distributed=True):
        folder = self.releases / version
        folder.mkdir()
        document = {"product": "Lineage 2 Chat Translator", "platform": "windows-x64",
                    "version": version, "payload": rows}
        if distributed:
            name = f"Lineage-2-Chat-Translator-{version}-Setup.exe"
            contents = ("synthetic installer " + version).encode()
            (folder / name).write_bytes(contents)
            document["installer"] = {"file": name, "bytes": len(contents),
                                     "sha256": hashlib.sha256(contents).hexdigest()}
        self.save_history(version, document)
        return document

    def save_history(self, version, document):
        (self.releases / version / "release-manifest.json").write_text(
            json.dumps(document), encoding="utf-8"
        )

    def plan(self):
        return layout.retirement_plan(self.current, self.releases, "1.3.0")

    def test_license_copy_ignores_old_notices_backups_and_nested_history(self):
        source = self.root / "license-source"
        source.mkdir()
        expected = {}
        for name in layout.LICENSE_FILES:
            expected[name] = ("notice " + name).encode()
            (source / name).write_bytes(expected[name])
        (source / "PYTHON.txt.old").write_bytes(b"old license backup")
        (source / "history").mkdir()
        (source / "history/old-installer.exe").write_bytes(b"historical installer")
        destination = self.app / "licenses"
        layout.copy_licenses(source, destination)
        self.assertEqual({path.name: path.read_bytes() for path in destination.iterdir()}, expected)
        self.assertTrue((source / "history/old-installer.exe").is_file())

    def test_missing_required_license_fails_before_copying_partial_notices(self):
        source = self.root / "license-source"
        source.mkdir()
        for name in layout.LICENSE_FILES[:-1]:
            (source / name).write_bytes(b"notice")
        destination = self.app / "licenses"
        with self.assertRaises(ValueError):
            layout.copy_licenses(source, destination)
        self.assertFalse(destination.exists())

    def test_payload_inventory_records_only_current_bytes_and_exact_assets(self):
        expected = {
            "Lineage2ChatTranslator.exe": b"current launcher",
            "USER-README.txt": b"current user readme",
            "THIRD-PARTY-NOTICES.txt": b"current notices",
            "_internal/python312.dll": b"current interpreter",
            "_internal/_tcl_data/encoding/cp1252.enc": b"current Tcl data",
            "_internal/assets/L2CT.ico": b"current icon",
            "_internal/assets/item_catalog.json": b"current catalog",
            "_internal/assets/" + layout.CLEANUP_PLAN_NAME: b"ownership metadata only",
            "licenses/PYTHON.txt": b"current license",
        }
        for name, contents in expected.items():
            self.write(name, contents)
        result = layout.payload_records(self.app, self.ASSETS)
        self.assertEqual({row["path"]: row for row in result},
                         {name: record(name, contents) for name, contents in expected.items()})

    def test_private_historical_and_unlisted_assets_block_packaging(self):
        for name in (
            "old-installer.exe", "release/1.0.0/launcher.exe",
            "_internal/release/1.0.0/old.dll", "_internal/.build/old.dll",
            "_internal/models/weights.bin", "_internal/logs/chat.txt",
            "_internal/settings.json", "_internal/runtime.json", "_internal/Network.log",
            "_internal/review.local.json", "_internal/library.dll.bak", "_internal/library.dll.old",
            "_internal/assets/old-item-catalog.json", "licenses/OLD-LICENSE.txt",
        ):
            with self.subTest(path=name):
                # Each case gets its own fixture to avoid one rejected file
                # hiding a failure to reject the next case.
                app = self.root / ("case-" + hashlib.sha256(name.encode()).hexdigest()[:8])
                candidate = app / name
                candidate.parent.mkdir(parents=True)
                candidate.write_bytes(b"must not enter installer")
                with self.assertRaises(ValueError):
                    layout.payload_records(app, self.ASSETS)

    def test_linked_dependency_cannot_enter_payload(self):
        external = self.root / "external.dll"
        external.write_bytes(b"unrelated file")
        dependency = self.app / "_internal/linked.dll"
        dependency.parent.mkdir()
        try:
            dependency.symlink_to(external)
        except OSError:
            self.skipTest("File symlink creation unavailable")
        with self.assertRaises(ValueError):
            layout.payload_records(self.app, self.ASSETS)
        self.assertEqual(external.read_bytes(), b"unrelated file")

    def test_reparse_attribute_is_rejected_even_without_symlink_mode(self):
        target = self.write("_internal/junction-like.dll")
        original = Path.lstat

        class ReparseState:
            def __init__(self, actual):
                self.st_mode = actual.st_mode
                self.st_file_attributes = 0x400

        def info(path, *args, **kwargs):
            actual = original(path, *args, **kwargs)
            return ReparseState(actual) if path == target else actual

        with patch.object(Path, "lstat", info), self.assertRaises(ValueError):
            layout.payload_records(self.app, self.ASSETS)

    def test_installer_file_list_contains_exact_records_without_recursive_glob(self):
        rows = [record("Lineage2ChatTranslator.exe"), record("_internal/current.dll"),
                record("licenses/PYTHON.txt")]
        destination = self.root / "payload.iss"
        layout.write_installer_file_list(destination, rows)
        directives = [line for line in destination.read_text().splitlines() if line.startswith("Source:")]
        self.assertEqual(directives, [
            'Source: "{#AppSource}\\Lineage2ChatTranslator.exe"; DestDir: "{app}"; Flags: ignoreversion',
            'Source: "{#AppSource}\\_internal\\current.dll"; DestDir: "{app}\\_internal"; Flags: ignoreversion',
            'Source: "{#AppSource}\\licenses\\PYTHON.txt"; DestDir: "{app}\\licenses"; Flags: ignoreversion',
        ])
        self.assertNotIn("*", "\n".join(directives))
        self.assertNotIn("recursesubdirs", "\n".join(directives))
        self.assertNotIn("createallsubdirs", "\n".join(directives))

    def test_unsafe_file_list_path_is_rejected_without_writing_include(self):
        for name in ("../private.txt", "C:/external.dll", "_internal/*.dll", '_internal/a";Flags: deleteafterinstall'):
            with self.subTest(path=name):
                destination = self.root / "unsafe.iss"
                with self.assertRaises(ValueError):
                    layout.write_installer_file_list(destination, [record(name)])
                self.assertFalse(destination.exists())

    def test_installer_verification_embeds_exact_hashes_and_escapes_pascal_paths(self):
        destination = self.root / 'verify.iss'
        rows = [record('Lineage2ChatTranslator.exe'), record("_internal/library's.txt")]
        layout.write_installer_verification(destination, rows)
        script = destination.read_text()
        self.assertIn('GetSHA256OfFile(FileName)', script)
        self.assertIn("_internal\\library''s.txt", script)
        for row in rows:
            self.assertIn(row['sha256'], script)
        destination.unlink()
        with self.assertRaises(ValueError):
            layout.write_installer_verification(destination, [{**rows[0], 'sha256': "'); malicious; ('"}])
        self.assertFalse(destination.exists())

    def test_retirement_plan_spans_skipped_releases_and_preserves_all_known_hashes(self):
        old_a = record("_internal/retired.dll", b"old payload A")
        old_b = record("_internal/RETIRED.dll", b"old payload B")
        old_other = record("licenses/RETIRED-NOTICE.txt", b"old notice")
        self.history("1.0.0", [old_a, old_other])
        self.history("1.1.0", [old_a])
        self.history("1.2.0", [old_b])
        plan = self.plan()
        by_path = {row["path"].casefold(): row for row in plan["obsolete_files"]}
        self.assertEqual(set(by_path), {"_internal/retired.dll", "licenses/retired-notice.txt"})
        self.assertEqual(by_path["_internal/retired.dll"]["hashes"], sorted({old_a["sha256"], old_b["sha256"]}))
        self.assertEqual(by_path["licenses/retired-notice.txt"]["hashes"], [old_other["sha256"]])
        self.assertIn("_internal/assets/" + layout.CLEANUP_PLAN_NAME, plan["current_paths"])
        self.assertNotIn("old payload", json.dumps(plan))
        self.assertNotIn("installer", json.dumps(plan))

    def test_current_case_only_and_ancestor_paths_never_become_retired(self):
        self.current += [record("_internal/new-directory/active.dll"), record("_internal/flattened")]
        self.history("1.0.0", [
            record("_INTERNAL/CURRENT.DLL", b"old case and bytes"),
            record("_internal/new-directory", b"formerly a file"),
            record("_internal/flattened/old.dll", b"formerly a directory child"),
            record("_internal/obsolete.dll", b"actually retired"),
        ])
        plan = self.plan()
        self.assertEqual([row["path"] for row in plan["obsolete_files"]], ["_internal/obsolete.dll"])

    def test_headless_unpublished_and_newer_builds_are_not_cleanup_provenance(self):
        self.history("1.0.0", [record("_internal/unpublished.dll")], distributed=False)
        for version in ("1.3.0", "1.4.0"):
            folder = self.releases / version
            folder.mkdir()
            (folder / "release-manifest.json").write_text("invalid newer manifest", encoding="utf-8")
        self.assertEqual(self.plan()["obsolete_files"], [])

    def test_retirement_provenance_rejects_changed_installer_bytes(self):
        old = self.history("1.0.0", [record("_internal/retired.dll")])
        installer = self.releases / "1.0.0" / old["installer"]["file"]
        original = installer.read_bytes()
        installer.write_bytes(b"X" * len(original))
        with self.assertRaisesRegex(ValueError, "verification"):
            self.plan()

    def test_retirement_provenance_rejects_identity_and_filename_mismatch(self):
        original = self.history("1.0.0", [record("_internal/retired.dll")])
        for field, value in (("product", "Other product"), ("platform", "other-platform"), ("version", "0.0.0")):
            with self.subTest(field=field):
                changed = dict(original)
                changed[field] = value
                self.save_history("1.0.0", changed)
                with self.assertRaisesRegex(ValueError, "identity"):
                    self.plan()
        changed = dict(original)
        changed["installer"] = dict(original["installer"], file="wrong-installer.exe")
        self.save_history("1.0.0", changed)
        with self.assertRaisesRegex(ValueError, "verification"):
            self.plan()

    def test_retirement_rejects_unsafe_or_duplicate_historical_paths(self):
        original = self.history("1.0.0", [record("_internal/retired.dll")])
        for rows in ([record("../outside.dll")],
                     [record("_internal/retired.dll"), record("_INTERNAL/RETIRED.DLL")],
                     [record("_internal/models/weights.bin")]):
            with self.subTest(rows=rows):
                self.save_history("1.0.0", dict(original, payload=rows))
                with self.assertRaises(ValueError):
                    self.plan()

    def test_history_manifest_read_is_bounded(self):
        self.history("1.0.0", [record("_internal/retired.dll")])
        with patch.object(layout, "MAX_HISTORY_MANIFEST_BYTES", 16), self.assertRaisesRegex(ValueError, "too large"):
            self.plan()


if __name__ == "__main__":
    unittest.main()
