"""Main-server exports use synthetic public files; no apps or network access."""
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile


_SPEC = importlib.util.spec_from_file_location(
    "l2_main_server_export", Path(__file__).resolve().parents[1] / "packaging/export_main_server.py"
)
exporter = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(exporter)


class SourcePublicationGateTests(unittest.TestCase):
    def test_public_source_must_succeed_before_bundle_export(self):
        order = []
        installer = {"file": "test.exe", "sha256": "b" * 64, "bytes": 123}
        source = {"repository": "https://github.com/example/source", "commit": "a" * 40,
                  "version": "1.2.3", "installer": installer}
        publisher = Mock(side_effect=lambda root: order.append("source") or source)
        revalidate = Mock(side_effect=lambda *a: order.append("revalidate"))
        with patch.object(exporter.release_site, "prepare_site", side_effect=lambda *a, **k: order.append("validate")), \
                patch.object(exporter, "_sibling", return_value=SimpleNamespace(publish_source=publisher, verify_source_current=revalidate)), \
                patch.object(exporter, "export_release", side_effect=lambda **k: order.append("export") or {"version": "1.2.3", "installer": installer}):
            result = exporter.publish_and_export()
        self.assertEqual(order, ["validate", "source", "export", "revalidate"])
        self.assertEqual(result["source"], source)
        publisher.assert_called_once_with(exporter.ROOT)
        revalidate.assert_called_once_with(exporter.ROOT, source)

    def test_release_change_after_source_publication_is_not_reported_as_ready(self):
        source = {"version": "1.2.3", "installer": {"sha256": "a" * 64}}
        for changed in ({"version": "1.2.4", "installer": source["installer"]},
                        {"version": "1.2.3", "installer": {"sha256": "b" * 64}}):
            with self.subTest(changed=changed), patch.object(exporter.release_site, "prepare_site"), \
                    patch.object(exporter, "_sibling", return_value=SimpleNamespace(publish_source=Mock(return_value=source))), \
                    patch.object(exporter, "export_release", return_value=changed):
                with self.assertRaisesRegex(ValueError, "Do not deploy"):
                    exporter.publish_and_export()

    def test_source_publication_failure_stops_export(self):
        publisher = Mock(side_effect=ValueError("Source publication could not be verified."))
        with patch.object(exporter.release_site, "prepare_site"), \
                patch.object(exporter, "_sibling", return_value=SimpleNamespace(publish_source=publisher)), \
                patch.object(exporter, "export_release") as export:
            with self.assertRaisesRegex(ValueError, "could not be verified"):
                exporter.publish_and_export()
        export.assert_not_called()

    def test_stale_website_or_invalid_destination_never_publishes_source(self):
        with patch.object(exporter, "_sibling") as source, patch.object(exporter, "export_release") as export:
            with self.assertRaises(ValueError):
                exporter.publish_and_export(base_url="http://example.test")
            with patch.object(exporter.release_site, "prepare_site", side_effect=ValueError("stale page")):
                with self.assertRaisesRegex(ValueError, "stale page"):
                    exporter.publish_and_export()
        source.assert_not_called()
        export.assert_not_called()

    def test_production_cli_uses_publication_gate(self):
        with patch("sys.argv", ["export_main_server.py"]), \
                patch.object(exporter, "publish_and_export", return_value={"source": "verified"}) as gate, \
                patch.object(exporter, "export_release") as offline, patch("builtins.print"):
            exporter.main()
        gate.assert_called_once_with(output_dir=None, base_url=exporter.DEFAULT_BASE_URL)
        offline.assert_not_called()


class MainServerExportTests(unittest.TestCase):
    OLD_ORIGIN = "https://old.example.test"
    SOURCES = ("app_version.py", "installer_runtime.py")
    ASSETS = ("assets/L2CT.ico", "assets/L2CT.png")
    TEMPLATE = '''<!doctype html><meta property="og:url" content="https://old.example.test/">
<link rel="canonical" href="https://old.example.test/">
<title>v{{VERSION}}</title><a href="/downloads/{{INSTALLER}}">Download</a>
<img src="/assets/L2CT.png"><a href="https://old.example.test/help">External unchanged</a>
<p>{{SIZE_MB}} MB; {{MODEL_NAME}} {{MODEL_DOWNLOAD_GB}} GB {{MODEL_STATUS}}</p>
<p>{{SIGNING_NOTICE}}</p><section>{{CHANGELOG}}</section>'''

    def setUp(self):
        self.enterContext(patch.object(exporter.release_site, "sign_metadata",
                                      side_effect=lambda data: hashlib.sha256(data).digest() * 12))
        self.enterContext(patch.object(exporter.release_site, "verify_signature",
                                      side_effect=self.verify_synthetic_signature))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.site = self.root / "sites/l2-chat-translator"
        (self.site / ".openai").mkdir(parents=True)
        (self.site / ".openai/hosting.json").write_text(json.dumps({
            "project_id": "synthetic-project", "static": {"directory": "dist"}
        }), encoding="utf-8")
        (self.site / "index.template.html").write_text(self.TEMPLATE, encoding="utf-8")
        (self.root / "assets").mkdir()
        for name in self.ASSETS:
            (self.root / name).write_bytes(b"synthetic icon")
        (self.root / "installer_runtime.py").write_text(
            'DEFAULT_MODEL = "test:9b"\n'
            'MODEL_CATALOG = {DEFAULT_MODEL: {"label": "Test 9B — Tested", '
            '"download_gb": 6.6, "tested": True}}\n', encoding="utf-8"
        )
        self.enterContext(patch.object(exporter, "SOURCES", self.SOURCES))
        self.enterContext(patch.object(exporter, "ASSETS", self.ASSETS))
        self.make_release("1.2.3", b"synthetic installer\x00\xff\r\n")
        self.prepare()

    @staticmethod
    def verify_synthetic_signature(data, signature):
        if signature != hashlib.sha256(data).digest() * 12:
            raise ValueError("Synthetic signature verification failed.")

    def make_release(self, version, content):
        (self.root / "app_version.py").write_text(f'VERSION = "{version}"\n', encoding="utf-8")
        folder = self.root / "release" / version
        folder.mkdir(parents=True)
        name = f"Lineage-2-Chat-Translator-{version}-Setup.exe"
        (folder / name).write_bytes(content)
        installer = {"file": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        manifest = {
            "product": "Lineage 2 Chat Translator", "version": version,
            "built_at_utc": "2026-11-08T00:00:00+00:00", "platform": "windows-x64",
            "authenticode_signed": False, "default_model": "test:9b",
            "models_bundled": False, "runtime_bundled": False,
            "packaged_self_test": {"ok": True, "frozen": True, "version": version},
            "sources": [{"path": name, "sha256": exporter.release_site.digest(self.root / name)}
                        for name in self.SOURCES],
            "assets": [{"path": name, "sha256": exporter.release_site.digest(self.root / name)}
                       for name in self.ASSETS],
            "installer": installer,
        }
        (folder / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (folder / "SHA256SUMS.txt").write_text(f"{installer['sha256']}  {name}\n", encoding="ascii")
        notes_path = self.site / "changelog.json"
        notes = json.loads(notes_path.read_text()) if notes_path.exists() else []
        notes.insert(0, {"version": version, "changes": ["Synthetic release."]})
        notes_path.write_text(json.dumps(notes), encoding="utf-8")
        return installer

    def prepare(self):
        return exporter.release_site.prepare_site(self.root, self.site, self.SOURCES, self.ASSETS)

    def export(self, **kwargs):
        return exporter.export_release(root=self.root, site=self.site, **kwargs)

    def snapshot(self):
        return {path.relative_to(self.site / "dist").as_posix(): path.read_bytes()
                for path in (self.site / "dist").rglob("*") if path.is_file()}

    def test_export_rewrites_only_metadata_origins_and_keeps_installer_bytes(self):
        before = self.snapshot()
        result = self.export()
        manifest = json.loads(Path(result["manifest"]).read_text())
        with zipfile.ZipFile(result["archive"]) as archive:
            after = {name: archive.read(name) for name in archive.namelist()}
        self.assertEqual(set(before), set(after))
        expected = before["index.html"].replace(
            b'content="https://old.example.test/"', b'content="https://40.160.140.227/"'
        ).replace(b'href="https://old.example.test/"', b'href="https://40.160.140.227/"')
        self.assertEqual(after["index.html"], expected)
        self.assertIn(b'href="/downloads/', after["index.html"])
        self.assertIn(b'src="/assets/L2CT.png"', after["index.html"])
        self.assertIn(b'href="https://old.example.test/help"', after["index.html"])
        for name in before.keys() - {"index.html"}:
            self.assertEqual(after[name], before[name], name)
        self.assertEqual(before, self.snapshot(), "The existing Site must not be modified.")
        self.assertEqual(manifest["version"], "1.2.3")
        self.assertEqual(manifest["base_url"], "https://40.160.140.227")
        self.assertEqual(manifest["installer"], result["installer"])
        self.assertRegex(manifest["release_id"], r"^1\.2\.3-[a-f0-9]{12}$")
        self.assertEqual(len(manifest["files"]), len(before))
        exporter.verify_archive(Path(result["archive"]), manifest)
        self.assertFalse(result["deployed"])

    def test_previous_version_download_is_preserved(self):
        old = self.snapshot()
        current = self.make_release("1.2.4", b"second synthetic installer")
        self.prepare()
        result = self.export()
        with zipfile.ZipFile(result["archive"]) as archive:
            old_name = "downloads/Lineage-2-Chat-Translator-1.2.3-Setup.exe"
            self.assertEqual(archive.read(old_name), old[old_name])
            self.assertEqual(archive.read("downloads/" + current["file"]), b"second synthetic installer")
        self.assertEqual(result["installer"], current)

    def test_repeated_export_has_same_id_and_bytes_but_different_origin_changes_id(self):
        first = self.export()
        first_zip = Path(first["archive"]).read_bytes()
        first_manifest = Path(first["manifest"]).read_bytes()
        second = self.export()
        self.assertEqual(first["release_id"], second["release_id"])
        self.assertEqual(first_zip, Path(second["archive"]).read_bytes())
        self.assertEqual(first_manifest, Path(second["manifest"]).read_bytes())
        changed = self.export(base_url="https://new.example.test/")
        self.assertNotEqual(first["release_id"], changed["release_id"])

    def test_invalid_base_urls_rejected_before_outputs_are_created(self):
        output = self.root / "export-test"
        for value in ("http://40.160.140.227", "ftp://host", "https://user:pass@host",
                      "https://host/path", "https://host?x=1", "https://host#fragment",
                      "https://host\n", "https://host:99999", "https://bad<host>",
                      "https://", "https://host:"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.export(base_url=value, output_dir=output)
        self.assertFalse(output.exists())

    def test_rewrite_preserves_whitespace_attribute_order_and_url_suffix(self):
        source = b'''<link href='https://old.test/guide?a=1&amp;b=2#x' rel='canonical' />\r\n<meta content="https://old.test/path" property="og:url">'''
        rewritten = exporter.rewrite_homepage(source, "https://40.160.140.227")
        self.assertEqual(rewritten, source.replace(b"https://old.test", b"https://40.160.140.227"))

    def test_missing_or_duplicate_metadata_is_rejected(self):
        for page in (b"<html></html>", (self.TEMPLATE + '<meta property="og:url" content="https://other.test/">').encode()):
            with self.subTest(page=page), self.assertRaisesRegex(ValueError, "exactly one"):
                exporter.rewrite_homepage(page, exporter.DEFAULT_BASE_URL)

    def test_private_file_and_stale_source_rejected_without_overwriting_previous_export(self):
        result = self.export()
        paths = [Path(result["archive"]), Path(result["manifest"])]
        before = [path.read_bytes() for path in paths]
        private = self.site / "dist/settings.json"
        private.write_text("private-marker", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unexpected public file"):
            self.export()
        private.unlink()
        (self.root / "app_version.py").write_text('VERSION = "1.2.3"\n# changed source', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Stale installer"):
            self.export()
        self.assertEqual([path.read_bytes() for path in paths], before)

    def test_failed_archive_verification_does_not_replace_outputs(self):
        result = self.export()
        paths = [Path(result["archive"]), Path(result["manifest"])]
        before = [path.read_bytes() for path in paths]
        with patch.object(exporter, "verify_archive", side_effect=ValueError("Synthetic corrupt archive")):
            with self.assertRaisesRegex(ValueError, "corrupt"):
                self.export()
        self.assertEqual([path.read_bytes() for path in paths], before)

    def test_archive_and_manifest_tampering_are_detected(self):
        result = self.export()
        path = Path(result["archive"])
        manifest = json.loads(Path(result["manifest"]).read_text())
        changed = json.loads(json.dumps(manifest))
        changed["files"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "Release ID"):
            exporter.verify_archive(path, changed)
        path.write_bytes(path.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "Archive checksum"):
            exporter.verify_archive(path, manifest)

    def test_recomputed_host_checksums_do_not_bypass_signature_or_installer_binding(self):
        for case in ("bad_signature", "different_signed_installer"):
            with self.subTest(case=case):
                result = self.export()
                path = Path(result["archive"])
                manifest = json.loads(Path(result["manifest"]).read_text())
                with zipfile.ZipFile(path) as archive:
                    content = {name: archive.read(name) for name in archive.namelist()}
                if case == "bad_signature":
                    content["downloads/release.sig"] = b"x" * 384
                else:
                    public = json.loads(content["downloads/release.json"])
                    public["installer"]["sha256"] = "0" * 64
                    metadata = json.dumps(public).encode()
                    content["downloads/release.json"] = metadata
                    # A valid signature for different release details still cannot
                    # authorize the installer selected in the archive manifest.
                    content["downloads/release.sig"] = hashlib.sha256(metadata).digest() * 12
                with zipfile.ZipFile(path, "w") as archive:
                    for name, data in content.items():
                        archive.writestr(name, data)
                manifest["files"] = [
                    {"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                    for name, data in content.items()]
                identity = {key: manifest[key] for key in
                            ("schema_version", "version", "base_url", "installer", "files")}
                manifest["release_id"] = (manifest["version"] + "-"
                                          + hashlib.sha256(exporter._canonical_json(identity)).hexdigest()[:12])
                manifest["archive"].update(bytes=path.stat().st_size, sha256=exporter.release_site.digest(path))
                with self.assertRaisesRegex(ValueError, "signature verification failed|Signed archive metadata differs"):
                    exporter.verify_archive(path, manifest)


if __name__ == "__main__":
    unittest.main()
