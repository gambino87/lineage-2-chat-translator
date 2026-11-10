"""Release/site consistency using synthetic files; no installers or apps execute."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


_SPEC = importlib.util.spec_from_file_location(
    "l2_release_site", Path(__file__).resolve().parents[1] / "packaging/release_site.py"
)
release_site = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(release_site)


class ReleaseSiteTests(unittest.TestCase):
    SOURCES = ("app_version.py", "installer_runtime.py", "app.py")
    ASSETS = ("assets/L2CT.ico", "assets/L2CT.png")
    TAG = "qwen-test:9b"
    TEMPLATE = """<!doctype html><title>v{{VERSION}}</title>
<a href="/downloads/{{INSTALLER}}">Download</a>
<a href="/downloads/{{INSTALLER}}">Download v{{VERSION}}</a>
<a href="/downloads/{{INSTALLER}}">Windows</a>
<a href="/downloads/{{INSTALLER}}">Download</a>
<p>{{SIZE_MB}} MB</p><section>{{CHANGELOG}}</section>
<p>{{MODEL_NAME}}: {{MODEL_DOWNLOAD_GB}} GB, {{MODEL_STATUS}}</p>
<p>{{SIGNING_NOTICE}}</p>"""

    def setUp(self):
        # These tests exercise the release lifecycle, not RSA. Native verification
        # is covered separately with real ephemeral keys in test_update_trust.
        self.signer = self.enterContext(patch.object(
            release_site, "sign_metadata", side_effect=lambda data: hashlib.sha256(data).digest() * 12))
        self.verifier = self.enterContext(patch.object(
            release_site, "verify_signature", side_effect=self.verify_synthetic_signature))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.site = self.root / "sites" / "translator"
        (self.site / ".openai").mkdir(parents=True)
        (self.site / ".openai/hosting.json").write_text(json.dumps({
            "project_id": "synthetic-project", "static": {"directory": "dist"}
        }), encoding="utf-8")
        (self.site / "index.template.html").write_text(self.TEMPLATE, encoding="utf-8")
        (self.root / "assets").mkdir()
        (self.root / "assets/L2CT.ico").write_bytes(b"synthetic ico")
        (self.root / "assets/L2CT.png").write_bytes(b"synthetic png")
        (self.root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.root / "installer_runtime.py").write_text(
            f'DEFAULT_MODEL = "{self.TAG}"\n'
            'MODEL_CATALOG = {DEFAULT_MODEL: {"label": "Qwen Test 9B — Tested", '
            '"download_gb": 6.6, "tested": True}}\n', encoding="utf-8"
        )
        self.make_release("1.2.3", b"synthetic installer one")

    @staticmethod
    def verify_synthetic_signature(data, signature):
        if signature != hashlib.sha256(data).digest() * 12:
            raise ValueError("Synthetic signature verification failed.")

    def make_release(self, version, contents):
        (self.root / "app_version.py").write_text(f'VERSION = "{version}"\n', encoding="utf-8")
        folder = self.root / "release" / version
        folder.mkdir(parents=True, exist_ok=True)
        filename = f"Lineage-2-Chat-Translator-{version}-Setup.exe"
        artifact = {"file": filename, "bytes": len(contents),
                    "sha256": hashlib.sha256(contents).hexdigest()}
        (folder / filename).write_bytes(contents)
        manifest = {
            "product": "Lineage 2 Chat Translator", "version": version,
            "built_at_utc": "2026-11-08T00:00:00+00:00", "platform": "windows-x64",
            "authenticode_signed": False, "default_model": self.TAG,
            "models_bundled": False, "runtime_bundled": False,
            "packaged_self_test": {"ok": True, "frozen": True, "version": version},
            "sources": [{"path": name, "sha256": release_site.digest(self.root / name)}
                        for name in self.SOURCES],
            "assets": [{"path": name, "sha256": release_site.digest(self.root / name)}
                       for name in self.ASSETS],
            "installer": artifact,
        }
        self.write_manifest(manifest, version)
        (folder / "SHA256SUMS.txt").write_text(
            f"{artifact['sha256']}  {filename}\n", encoding="ascii"
        )
        notes_path = self.site / "changelog.json"
        notes = json.loads(notes_path.read_text(encoding="utf-8")) if notes_path.exists() else []
        notes = [entry for entry in notes if entry["version"] != version]
        notes.append({"version": version, "changes": [f"Synthetic change for {version}."]})
        notes.sort(key=lambda entry: tuple(map(int, entry["version"].split("."))), reverse=True)
        self.write_changelog(notes)
        return manifest

    def write_changelog(self, entries):
        (self.site / "changelog.json").write_text(json.dumps(entries), encoding="utf-8")

    def read_manifest(self, version="1.2.3"):
        return json.loads((self.root / "release" / version / "release-manifest.json").read_text())

    def write_manifest(self, manifest, version="1.2.3"):
        (self.root / "release" / version / "release-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )

    def prepare(self, **kwargs):
        return release_site.prepare_site(self.root, self.site, self.SOURCES, self.ASSETS, **kwargs)

    def add_incremental(self, manifest):
        version = manifest["version"]
        app = self.root / ("payload-" + version)
        app.mkdir()
        contents = b"MZ synthetic executable " + version.encode()
        (app / "Lineage2ChatTranslator.exe").write_bytes(contents)
        manifest["payload"] = [{"path": "Lineage2ChatTranslator.exe", "bytes": len(contents),
                                "sha256": hashlib.sha256(contents).hexdigest()}]
        cleanup = {"schema_version": 1, "product": "Lineage 2 Chat Translator", "version": version,
                   "current_paths": ["Lineage2ChatTranslator.exe"], "obsolete_files": []}
        manifest["incremental"] = release_site.update_tools().create_package(
            app, self.root / "release" / version, version, manifest["payload"], cleanup)
        self.write_manifest(manifest, version)
        return manifest

    def test_file_update_metadata_and_objects_are_published_and_retained(self):
        old = self.add_incremental(self.read_manifest())
        self.prepare()
        before = {name: data for name, data in self.public_snapshot().items() if name.startswith("updates/")}
        current = self.add_incremental(self.make_release("1.2.4", b"new synthetic installer"))
        self.prepare()
        after = self.public_snapshot()
        self.assertEqual(before, {name: after[name] for name in before})
        public = json.loads(after["downloads/release.json"])
        self.assertEqual(public["incremental"], current["incremental"])
        self.assertIn(old["incremental"]["manifest"]["file"], after)
        self.assertTrue(self.prepare(check=True)["checked_only"])

    def test_modified_retained_update_object_is_rejected_before_replacement(self):
        self.add_incremental(self.read_manifest())
        self.prepare()
        blob = next((self.site / "dist/updates/blobs").glob("*.gz"))
        blob.write_bytes(b"modified")
        self.assert_rejected_without_changes()

    def test_update_manifest_cannot_disagree_with_verified_installer_payload(self):
        manifest = self.add_incremental(self.read_manifest())
        manifest["payload"][0]["sha256"] = "0" * 64
        self.write_manifest(manifest)
        self.assert_rejected_without_changes()

    def public_snapshot(self):
        return {path.relative_to(self.site / "dist").as_posix(): path.read_bytes()
                for path in (self.site / "dist").rglob("*") if path.is_file()}

    def assert_rejected_without_changes(self):
        before = self.public_snapshot()
        with self.assertRaises((ValueError, OSError)):
            self.prepare()
        self.assertEqual(before, self.public_snapshot())

    def test_new_release_updates_all_fields_and_preserves_previous_download(self):
        self.prepare()
        old_name = "Lineage-2-Chat-Translator-1.2.3-Setup.exe"
        old_bytes = (self.site / "dist/downloads" / old_name).read_bytes()
        manifest = self.make_release("1.2.4", b"synthetic new installer" * 5_000)
        result = self.prepare()
        page = (self.site / "dist/index.html").read_text(encoding="utf-8")
        artifact = manifest["installer"]
        self.assertEqual(page.count(f'/downloads/{artifact["file"]}'), 4)
        self.assertIn("v1.2.4", page)
        self.assertIn("Synthetic change for 1.2.3.", page)
        self.assertNotIn(f'/downloads/{old_name}', page)
        self.assertIn(f'{artifact["bytes"] / 1_000_000:.1f} MB', page)
        self.assertNotIn(artifact["sha256"], page)
        self.assertNotIn("{{", page)
        self.assertEqual(old_bytes, (self.site / "dist/downloads" / old_name).read_bytes())
        self.assertEqual(result["retained_installers"], 2)
        checksums = (self.site / "dist/downloads/SHA256SUMS.txt").read_text()
        self.assertIn(f'{hashlib.sha256(old_bytes).hexdigest()}  {old_name}', checksums)
        self.assertIn(f'{artifact["sha256"]}  {artifact["file"]}', checksums)
        public = json.loads((self.site / "dist/downloads/release.json").read_text())
        self.assertEqual(public["version"], "1.2.4")
        self.assertEqual(public["installer"], artifact)

    def test_server_preparation_does_not_require_a_sites_hosting_identity(self):
        (self.site / ".openai/hosting.json").unlink()
        result = self.prepare()
        self.assertEqual(result["version"], "1.2.3")
        self.assertNotIn("project_id", result)
        self.assertTrue(self.prepare(check=True)["checked_only"])

    def test_identical_preparation_is_idempotent_and_check_does_not_write_dist(self):
        self.prepare()
        snapshot = self.public_snapshot()
        self.prepare()
        self.assertEqual(snapshot, self.public_snapshot())
        modified = {path: (self.site / "dist" / path).stat().st_mtime_ns for path in snapshot}
        result = self.prepare(check=True)
        self.assertTrue(result["checked_only"])
        self.assertEqual(snapshot, self.public_snapshot())
        self.assertEqual(modified, {path: (self.site / "dist" / path).stat().st_mtime_ns
                                    for path in snapshot})
        self.assertFalse(result["published"])

    def test_check_detects_stale_homepage_without_rewriting_it(self):
        self.prepare()
        path = self.site / "dist/index.html"
        path.write_text("old content", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "needs preparation"):
            self.prepare(check=True)
        self.assertEqual(path.read_text(), "old content")

    def test_changed_source_is_rejected_before_public_files_change(self):
        self.prepare()
        (self.root / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        self.assert_rejected_without_changes()

    def test_changed_icon_is_rejected_before_public_files_change(self):
        self.prepare()
        (self.root / "assets/L2CT.ico").write_bytes(b"changed icon")
        self.assert_rejected_without_changes()

    def test_corrupt_installer_is_rejected_before_public_files_change(self):
        self.prepare()
        (self.root / "release/1.2.3/Lineage-2-Chat-Translator-1.2.3-Setup.exe").write_bytes(b"corrupt")
        self.assert_rejected_without_changes()

    def test_failed_or_mismatched_packaged_self_test_is_rejected(self):
        self.prepare()
        original = self.read_manifest()
        for changes in ({"ok": False}, {"frozen": False}, {"version": "0.0.1"}):
            with self.subTest(changes=changes):
                manifest = json.loads(json.dumps(original))
                manifest["packaged_self_test"].update(changes)
                self.write_manifest(manifest)
                self.assert_rejected_without_changes()

    def test_existing_version_cannot_be_replaced_with_different_bytes(self):
        self.prepare()
        self.make_release("1.2.3", b"replacement with matching new manifest")
        self.assert_rejected_without_changes()

    def test_unsafe_installer_names_are_rejected(self):
        original = self.read_manifest()
        for filename in ("../outside.exe", "C:\\outside.exe", "other.exe"):
            with self.subTest(filename=filename):
                manifest = json.loads(json.dumps(original))
                manifest["installer"]["file"] = filename
                self.write_manifest(manifest)
                self.assert_rejected_without_changes()

    def test_unexpected_public_file_is_rejected_without_deleting_it(self):
        self.prepare()
        private = self.site / "dist/settings.json"
        private.write_text('{"secret": "must-not-publish"}', encoding="utf-8")
        self.assert_rejected_without_changes()
        self.assertTrue(private.is_file())

    def test_private_fields_and_local_files_do_not_enter_public_output(self):
        manifest = self.read_manifest()
        manifest["private"] = {"password": "private-marker"}
        manifest["payload"] = [{"path": "C:/Users/private-marker/file"}]
        manifest["packaged_self_test"]["settings_path"] = "C:/private-marker/settings.json"
        manifest["installer"]["private_path"] = "C:/private-marker/download.exe"
        self.write_manifest(manifest)
        (self.root / "settings.json").write_text("private-marker")
        (self.root / "Network.log").write_text("private-marker")
        self.prepare()
        public = self.public_snapshot()
        self.assertEqual(len(public), 9)
        self.assertFalse(any(b"private-marker" in content for content in public.values()))
        details = json.loads(public["downloads/release.json"])
        self.assertNotIn("payload", details)
        self.assertNotIn("packaged_self_test", details)
        self.assertEqual(set(details["installer"]), {"file", "bytes", "sha256"})

    def test_template_missing_release_value_is_rejected(self):
        self.prepare()
        template = self.TEMPLATE.replace("{{VERSION}}", "old version")
        (self.site / "index.template.html").write_text(template, encoding="utf-8")
        self.assert_rejected_without_changes()

    def test_template_stale_download_link_is_rejected(self):
        self.prepare()
        template = self.TEMPLATE.replace("/downloads/{{INSTALLER}}", "/downloads/old.exe", 1)
        (self.site / "index.template.html").write_text(template, encoding="utf-8")
        self.assert_rejected_without_changes()

    def test_missing_source_allowlist_entry_is_rejected(self):
        self.prepare()
        manifest = self.read_manifest()
        manifest["sources"].pop()
        self.write_manifest(manifest)
        self.assert_rejected_without_changes()

    def test_missing_current_changelog_notes_leave_previous_release_unchanged(self):
        self.prepare()
        self.make_release("1.2.4", b"new synthetic installer")
        self.write_changelog([{"version": "1.2.3", "changes": ["Previous release."]}])
        self.assert_rejected_without_changes()

    def test_missing_changelog_file_leaves_previous_release_unchanged(self):
        self.prepare()
        (self.site / "changelog.json").unlink()
        self.assert_rejected_without_changes()

    def test_invalid_changelog_entries_are_rejected_without_changing_public_files(self):
        self.prepare()
        current = {"version": "1.2.3", "changes": ["Current change."]}
        invalid = [
            [], {}, ["not an entry"],
            [{"version": "1.2.3", "changes": []}],
            [{"version": "1.2.3", "changes": ["   "]}],
            [{"version": "1.2.3", "changes": [None]}],
            [{"version": "1.2.3", "changes": "not a list"}],
            [{"version": "1.2.3", "changes": ["A note"], "private": "extra field"}],
            [{"version": "not-a-version", "changes": ["A note"]}],
            [current, current],
            [current, {"version": "01.2.3", "changes": ["Numeric duplicate."]}],
            [{"version": "1.2.2", "changes": ["Older first."]}, current],
        ]
        for entries in invalid:
            with self.subTest(entries=entries):
                self.write_changelog(entries)
                self.assert_rejected_without_changes()

    def test_changelog_note_markup_is_escaped_once(self):
        self.write_changelog([{"version": "1.2.3", "changes": [
            '<script>alert("example")</script> & <img src=x onerror=alert(1)>'
        ]}])
        self.prepare()
        page = (self.site / "dist/index.html").read_text(encoding="utf-8")
        self.assertIn('&lt;script&gt;alert(&quot;example&quot;)&lt;/script&gt; &amp;', page)
        self.assertIn('&lt;img src=x onerror=alert(1)&gt;', page)
        self.assertNotIn("<script>", page)
        self.assertNotIn("<img ", page)
        self.assertNotIn("&amp;lt;", page)

    def test_numeric_release_order_and_older_history_render_correctly(self):
        self.make_release("1.10.0", b"newest synthetic installer")
        self.write_changelog([
            {"version": "1.10.0", "changes": ["Latest changes."]},
            {"version": "1.9.0", "changes": ["Previous changes."]},
            {"version": "1.2.3", "changes": ["Original changes."]},
        ])
        self.prepare()
        page = (self.site / "dist/index.html").read_text(encoding="utf-8")
        earlier = page.index('<details class="earlier-releases">')
        self.assertLess(page.index("Latest changes."), page.index("Previous changes."))
        self.assertLess(page.index("Previous changes."), earlier)
        self.assertGreater(page.index("Original changes."), earlier)
        self.assertEqual(page.count('class="current-release"'), 1)
        self.assertIn('v1.10.0 <span class="current-release">Current</span>', page)

    def test_checksum_stays_in_metadata_and_file_but_not_homepage(self):
        self.prepare()
        files = self.public_snapshot()
        artifact = self.read_manifest()["installer"]
        checksum = artifact["sha256"]
        self.assertNotIn(checksum.encode("ascii"), files["index.html"])
        self.assertNotIn(b"SHA-256", files["index.html"])
        self.assertIn(f"{checksum}  {artifact['file']}", files["downloads/SHA256SUMS.txt"].decode("ascii").splitlines())
        self.assertEqual(json.loads(files["downloads/release.json"])["installer"]["sha256"], checksum)
        self.assertEqual(hashlib.sha256(files["downloads/" + artifact["file"]]).hexdigest(), checksum)

    def test_failed_replacement_restores_original_public_files(self):
        self.prepare()
        before = self.public_snapshot()
        self.make_release("1.2.4", b"new synthetic installer")
        original_rename = Path.rename
        scratch = self.site / ".sites-artifacts"

        def rename(path, target):
            if path.name == "dist" and path.parent.parent == scratch:
                raise PermissionError("synthetic replacement denied")
            return original_rename(path, target)

        with patch.object(Path, "rename", rename):
            with self.assertRaises(PermissionError):
                self.prepare()
        self.assertEqual(before, self.public_snapshot())

    def test_failed_rollback_keeps_backup_after_temporary_cleanup(self):
        self.prepare()
        before = self.public_snapshot()
        self.make_release("1.2.4", b"new synthetic installer")
        original_rename = Path.rename
        scratch = self.site / ".sites-artifacts"

        def rename(path, target):
            if ((path.name == "dist" and path.parent.parent == scratch)
                    or (path.parent == scratch and path.name.endswith("-previous"))):
                raise PermissionError("synthetic replacement and rollback denied")
            return original_rename(path, target)

        with patch.object(Path, "rename", rename):
            with self.assertRaisesRegex(OSError, "retained"):
                self.prepare()
        backups = list(scratch.glob("*-previous"))
        self.assertEqual(len(backups), 1)
        retained = {path.relative_to(backups[0]).as_posix(): path.read_bytes()
                    for path in backups[0].rglob("*") if path.is_file()}
        self.assertEqual(before, retained)

    def test_signature_covers_exact_public_bytes_and_repreparation_never_resigns(self):
        self.prepare()
        folder = self.root / "release/1.2.3"
        public = self.public_snapshot()
        self.assertEqual((folder / "release.json").read_bytes(), public["downloads/release.json"])
        self.assertEqual((folder / "release.sig").read_bytes(), public["downloads/release.sig"])
        self.verify_synthetic_signature(public["downloads/release.json"], public["downloads/release.sig"])
        self.assertNotIn(b"\r\n", public["downloads/release.json"])
        self.prepare()
        self.prepare(check=True)
        self.signer.assert_called_once()

    def test_check_missing_signature_never_uses_signing_key(self):
        with self.assertRaisesRegex(ValueError, "Signed release is missing"):
            self.prepare(check=True)
        self.signer.assert_not_called()
        self.assertFalse((self.root / "release/1.2.3/release.json").exists())

    def test_missing_key_or_failed_signer_leaves_public_files_unchanged(self):
        self.prepare()
        before = self.public_snapshot()
        self.make_release("1.2.4", b"synthetic second release")
        self.signer.side_effect = ValueError("Signing key is unavailable.")
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.prepare()
        self.assertEqual(before, self.public_snapshot())
        self.assertFalse((self.root / "release/1.2.4/release.json").exists())

    def test_invalid_signer_output_is_not_written(self):
        for signature in (b"short", b"x" * 384):
            with self.subTest(signature_size=len(signature)):
                self.signer.side_effect = None
                self.signer.return_value = signature
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertFalse((self.root / "release/1.2.3/release.json").exists())

    def test_tampered_or_missing_stored_signature_is_not_repaired_by_resigning(self):
        self.prepare()
        folder = self.root / "release/1.2.3"
        signature = (folder / "release.sig").read_bytes()
        for replacement in (b"short", b"x" * 384, None):
            with self.subTest(signature_size=None if replacement is None else len(replacement)):
                if replacement is None:
                    (folder / "release.sig").unlink()
                else:
                    (folder / "release.sig").write_bytes(replacement)
                self.assert_rejected_without_changes()
                self.signer.assert_called_once()
                (folder / "release.sig").write_bytes(signature)

    def test_same_version_metadata_change_is_rejected_even_with_valid_installer(self):
        self.prepare()
        manifest = self.read_manifest()
        manifest["built_at_utc"] = "2026-11-09T00:00:00+00:00"
        self.write_manifest(manifest)
        with self.assertRaisesRegex(ValueError, "Signed release metadata changed"):
            self.prepare()
        self.signer.assert_called_once()

    def test_export_check_rejects_missing_or_tampered_public_signature(self):
        self.prepare()
        signature_file = self.site / "dist/downloads/release.sig"
        signature = signature_file.read_bytes()
        for replacement in (b"x" * 384, None):
            with self.subTest(missing=replacement is None):
                if replacement is None:
                    signature_file.unlink()
                else:
                    signature_file.write_bytes(replacement)
                with self.assertRaisesRegex(ValueError, "needs preparation"):
                    self.prepare(check=True)
                self.signer.assert_called_once()
                signature_file.write_bytes(signature)


if __name__ == "__main__":
    unittest.main()
