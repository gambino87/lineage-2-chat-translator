"""Publication gate tests use synthetic bytes only; no network, apps or deletion."""
from contextlib import nullcontext
import hashlib
import gzip
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location("l2_complete_release", Path(__file__).resolve().parents[1] / "packaging/complete_release.py")
complete = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(complete)


class HostVerificationTests(unittest.TestCase):
    def setUp(self):
        self.base = "https://release.example.test"
        self.binary = b"synthetic installer, never executed"
        self.installer = {"file": "Lineage-2-Chat-Translator-1.2.3-Setup.exe",
                          "bytes": len(self.binary), "sha256": hashlib.sha256(self.binary).hexdigest()}
        self.metadata = json.dumps({"version": "1.2.3", "installer": self.installer}).encode()
        self.signature = b"s" * 384
        self.page = b"<html><body>Version 1.2.3</body></html>"
        self.responses = {"/": self.page, "/downloads/release.json": self.metadata,
                          "/downloads/release.sig": self.signature,
                          "/downloads/" + self.installer["file"]: self.binary,
                          "/downloads/SHA256SUMS.txt": (self.installer["sha256"] + "  " + self.installer["file"] + "\n").encode()}
        self.fetch = self.enterContext(patch.object(complete, "fetch_https", side_effect=lambda url, limit: self.responses[url.removeprefix(self.base)]))
        self.signature_check = self.enterContext(patch.object(complete.release_site, "verify_signature"))

    def verify(self, **kwargs):
        return complete.verify_host(self.base, self.page, self.metadata, self.signature, self.installer, mirror=kwargs.get("mirror", False))

    def test_all_public_bytes_checked_without_execution(self):
        self.assertTrue(self.verify()["installer_verified"])
        self.assertEqual(self.fetch.call_count, 5)
        self.signature_check.assert_called_once_with(self.metadata, self.signature)

    def add_incremental(self):
        raw = b"MZ synthetic application"
        compressed = gzip.compress(raw, mtime=0)
        row = {"path": "Lineage2ChatTranslator.exe", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
               "blob": {"bytes": len(compressed), "sha256": hashlib.sha256(compressed).hexdigest()}}
        document = {"schema_version": 1, "product": "Lineage 2 Chat Translator", "platform": "windows-x64", "version": "1.2.3",
                    "files": [row], "cleanup": {"schema_version": 1, "product": "Lineage 2 Chat Translator", "version": "1.2.3",
                        "current_paths": [row["path"]], "obsolete_files": []}}
        data = json.dumps(document).encode()
        descriptor = {"schema_version": 1, "manifest": {"file": "updates/1.2.3/files.json", "bytes": len(data),
                                                        "sha256": hashlib.sha256(data).hexdigest()}}
        public = {"version": "1.2.3", "installer": self.installer, "incremental": descriptor}
        self.metadata = json.dumps(public).encode()
        self.responses["/downloads/release.json"] = self.metadata
        self.responses["/updates/1.2.3/files.json"] = data
        blob_path = "/updates/blobs/" + row["blob"]["sha256"] + ".gz"
        self.responses[blob_path] = compressed
        return blob_path

    def test_every_live_incremental_object_is_verified_before_completion(self):
        blob_path = self.add_incremental()
        result = self.verify()
        self.assertTrue(result["incremental_verified"])
        self.assertEqual(result["incremental_files"], 1)
        self.assertEqual(self.fetch.call_count, 7)
        self.responses[blob_path] = b"changed compressed object"
        with self.assertRaisesRegex(ValueError, "checksum/size"):
            self.verify()

    def test_stale_metadata_stops_before_installer_download(self):
        self.responses["/downloads/release.json"] = b'{"version":"1.2.2"}'
        with self.assertRaisesRegex(ValueError, "different release"):
            self.verify()
        self.assertEqual(self.fetch.call_count, 2)

    def test_invalid_signature_stops_before_installer_download(self):
        self.signature_check.side_effect = ValueError("invalid signature")
        with self.assertRaisesRegex(ValueError, "invalid signature"):
            self.verify()
        self.assertEqual(self.fetch.call_count, 2)

    def test_changed_installer_checksum_or_homepage_rejects_release(self):
        for path, replacement in (("/downloads/" + self.installer["file"], b"untrusted bytes"),
                                  ("/downloads/SHA256SUMS.txt", b"wrong digest"),
                                  ("/", b"<html><body>Version 1.2.2</body></html>")):
            with self.subTest(path=path), patch.dict(self.responses, {path: replacement}):
                with self.assertRaises(ValueError):
                    self.verify()

    def test_only_mirror_can_add_bounded_provider_challenge(self):
        extra = b"<script>/* /cdn-cgi/challenge-platform/ */</script>"
        self.responses["/"] = self.page.replace(b"</body>", extra + b"</body>")
        self.assertTrue(self.verify(mirror=True)["homepage_verified"])
        with self.assertRaisesRegex(ValueError, "Homepage differs"):
            self.verify(mirror=False)
        self.responses["/"] = self.responses["/"].replace(b"1.2.3", b"1.2.2")
        with self.assertRaises(ValueError):
            self.verify(mirror=True)

    def test_extra_or_oversized_script_is_not_accepted(self):
        for extra in (b"<script>/cdn-cgi/challenge-platform/</script><script>x</script>",
                      b"<script>/cdn-cgi/challenge-platform/" + b"x" * 16384 + b"</script>"):
            self.assertFalse(complete.page_matches(self.page.replace(b"</body>", extra + b"</body>"),
                                                  self.page, allow_provider_script=True))


class CompletionGateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        folder = self.root / "release/1.2.3"
        folder.mkdir(parents=True)
        (folder / "release.json").write_bytes(b"synthetic metadata")
        (folder / "release.sig").write_bytes(b"s" * 384)
        self.site = self.root / "sites/l2-chat-translator/dist"
        self.site.mkdir(parents=True)
        (self.site / "index.html").write_bytes(b"synthetic homepage")
        (self.root / ".build").mkdir()
        self.latest = self.root / ".build/1.2.3-20260101T010101Z"
        (self.root / ".build/latest-build.json").write_text(json.dumps({"build": str(self.latest)}))
        self.prepared = {"version": "1.2.3", "installer": {"file": "synthetic.exe"}}
        self.order = []
        self.prepare = self.enterContext(patch.object(complete.release_site, "prepare_site", return_value=self.prepared))
        self.retained = self.enterContext(patch.object(complete, "verify_retained_inputs", return_value={"verified": True}))
        self.source = self.enterContext(patch.object(complete, "verify_public_source", side_effect=lambda *a: self.order.append("source") or {"commit": "a" * 40}))
        self.host = self.enterContext(patch.object(complete, "verify_host", side_effect=lambda *a, **k: self.order.append("mirror" if k["mirror"] else "main") or {"url": a[0]}))
        self.enterContext(patch.object(complete, "rewrite_homepage", side_effect=lambda page, url: page))
        self.recheck = self.enterContext(patch.object(complete.source_release, "verify_source_current", side_effect=lambda *a: self.order.append("recheck")))
        self.cleanup = self.enterContext(patch.object(complete, "cleanup_release", side_effect=lambda *a: self.order.append("cleanup") or {"removed": [], "removed_bytes": 0}))
        self.plan = self.enterContext(patch.object(complete, "plan_cleanup", return_value=[]))
        self.lock = self.enterContext(patch.object(complete, "release_lock", return_value=nullcontext()))

    def test_cleanup_runs_automatically_only_after_every_gate_and_recheck(self):
        report = complete.complete_release(self.root)
        self.assertEqual(self.order, ["source", "main", "recheck", "cleanup"])
        self.host.assert_called_once()
        self.assertEqual(report["hosts"], [{"url": complete.DEFAULT_BASE_URL}])
        self.cleanup.assert_called_once_with(self.root, "1.2.3", self.latest)
        self.assertFalse(report["models_touched"])
        self.assertTrue((self.root / ".build/release-completion-1.2.3.local.json").is_file())

    def test_source_failure_keeps_everything_for_retry(self):
        self.source.side_effect = ValueError("source not published")
        with self.assertRaises(ValueError):
            complete.complete_release(self.root)
        self.host.assert_not_called()
        self.cleanup.assert_not_called()

    def test_missing_required_backend_preserves_caches(self):
        self.retained.side_effect = ValueError("backend missing")
        with self.assertRaisesRegex(ValueError, "backend missing"):
            complete.complete_release(self.root)
        self.source.assert_not_called()
        self.cleanup.assert_not_called()

    def test_unfinished_server_blocks_all_cleanup(self):
        self.host.side_effect = ValueError("main not live")
        with self.assertRaisesRegex(ValueError, "main not live"):
            complete.complete_release(self.root)
        self.cleanup.assert_not_called()

    def test_local_edits_during_verification_keep_artifacts(self):
        self.prepare.side_effect = [self.prepared, {**self.prepared, "version": "1.2.4"}]
        with self.assertRaisesRegex(ValueError, "changed during"):
            complete.complete_release(self.root)
        self.cleanup.assert_not_called()

    def test_source_changes_after_public_checks_block_cleanup(self):
        self.recheck.side_effect = ValueError("source changed")
        with self.assertRaises(ValueError):
            complete.complete_release(self.root)
        self.cleanup.assert_not_called()

    def test_dry_run_never_deletes_and_still_requires_live_gates(self):
        self.assertTrue(complete.complete_release(self.root, dry_run=True)["cleanup"]["dry_run"])
        self.assertEqual(self.order, ["source", "main", "recheck"])
        self.plan.assert_called_once_with(self.root, "1.2.3", self.latest)
        self.cleanup.assert_not_called()

    def test_cli_defaults_to_verified_automatic_cleanup(self):
        with patch("sys.argv", ["complete_release.py"]), patch.object(complete, "complete_release", return_value={}) as finish, patch("builtins.print"):
            complete.main()
        finish.assert_called_once_with(dry_run=False)


class SourceVerificationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.document = {"version": "1.2.3", "application_sources": [], "application_assets": [],
                         "published_installer": {"sha256": "b" * 64}}
        self.tagged = dict(self.document)
        self.receipt = {"commit": "a" * 40, "public_verified": True}
        self.enterContext(patch.object(complete.source_release, "_state", return_value=self.receipt))
        self.enterContext(patch.object(complete.source_release, "verify_source_current"))
        self.enterContext(patch.object(complete.source_release, "snapshot", return_value=({"app.py": b"source"}, self.document)))
        self.enterContext(patch.object(complete.source_release, "safe_path", return_value=self.root))
        self.public = self.enterContext(patch.object(complete.source_release, "_verify_public"))
        self.remote_tag = "c" * 40 + "\trefs/tags/v1.2.3"
        def git(repo, *args):
            if args[0] == "remote":
                return complete.source_release.REMOTE
            if args[0] == "rev-parse":
                return "c" * 40
            if args[0] == "ls-remote":
                return self.remote_tag
            if args[0] == "show":
                return json.dumps(self.tagged)
            raise AssertionError(args)
        self.enterContext(patch.object(complete.source_release, "_git", side_effect=git))

    def test_saved_success_receipt_still_requires_fresh_public_source_and_tag(self):
        self.assertEqual(complete.verify_public_source(self.root), self.receipt)
        self.public.assert_called_once_with("a" * 40, {"app.py": b"source"})

    def test_failed_live_source_check_cannot_reuse_old_success(self):
        self.public.side_effect = ValueError("GitHub unavailable")
        with self.assertRaisesRegex(ValueError, "GitHub unavailable"):
            complete.verify_public_source(self.root)

    def test_changed_remote_tag_or_tagged_application_blocks_completion(self):
        self.remote_tag = "d" * 40 + "\trefs/tags/v1.2.3"
        with self.assertRaisesRegex(ValueError, "tag differs"):
            complete.verify_public_source(self.root)
        self.remote_tag = "c" * 40 + "\trefs/tags/v1.2.3"
        self.tagged["application_sources"] = [{"path": "changed.py"}]
        with self.assertRaisesRegex(ValueError, "does not match"):
            complete.verify_public_source(self.root)


class RetainedInputTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.backend = self.root / ".build-tools/secure-ollama/output/ollama.exe"
        self.backend.parent.mkdir(parents=True)
        self.backend.write_bytes(b"verified synthetic frontend")
        inventory = {"files": [{"path": "ollama.exe", "bytes": self.backend.stat().st_size,
                                 "sha256": hashlib.sha256(self.backend.read_bytes()).hexdigest()}]}
        (self.root / "assets").mkdir()
        (self.root / "assets/secure-ollama-manifest.json").write_text(json.dumps(inventory))
        self.tools = [self.root / ".build-tools/venv/Scripts/python.exe",
                      self.root / ".build-tools/inno-portable/tools/ISCC.exe"]
        for tool in self.tools:
            tool.parent.mkdir(parents=True)
            tool.write_bytes(b"never executed")
        folder = self.root / "release/1.2.3"
        folder.mkdir(parents=True)
        (folder / "release-manifest.json").write_text('{"payload": []}')
        self.history = self.enterContext(patch.object(complete, "retirement_plan", return_value={"obsolete_files": []}))

    def test_verified_backend_tools_and_history_are_required(self):
        self.assertTrue(complete.verify_retained_inputs(self.root, "1.2.3")["historical_installers_verified"])
        self.history.assert_called_once_with([], self.root / "release", "1.2.4")

    def test_missing_or_changed_frontend_prevents_deleting_rebuild_cache(self):
        self.backend.write_bytes(b"changed synthetic frontend")
        with self.assertRaisesRegex(ValueError, "verified compiled backend"):
            complete.verify_retained_inputs(self.root, "1.2.3")
        self.backend.unlink()
        with self.assertRaises(ValueError):
            complete.verify_retained_inputs(self.root, "1.2.3")
        self.history.assert_not_called()

    def test_missing_release_tool_or_corrupt_historical_installer_blocks_cleanup(self):
        self.tools[0].unlink()
        with self.assertRaisesRegex(ValueError, "required release tool"):
            complete.verify_retained_inputs(self.root, "1.2.3")
        self.tools[0].write_bytes(b"never executed")
        self.history.side_effect = ValueError("Historical installer failed verification")
        with self.assertRaisesRegex(ValueError, "Historical installer"):
            complete.verify_retained_inputs(self.root, "1.2.3")


class FetchBoundaryTests(unittest.TestCase):
    def test_system_curl_has_bounded_time_size_and_no_user_config_or_redirect(self):
        with tempfile.TemporaryDirectory() as temporary:
            system = Path(temporary)
            (system / "curl.exe").write_bytes(b"synthetic placeholder, never executed")
            with patch.object(complete, "windows_directories", return_value=(system, system)), \
                 patch.object(complete.subprocess, "run", return_value=Mock(stdout=b"ok")) as run:
                self.assertEqual(complete.fetch_https("https://example.test/release", 10), b"ok")
            args = run.call_args.args[0]
            self.assertEqual(args[:2], [str(system / "curl.exe"), "--disable"])
            for option in ("--max-filesize", "--max-time", "--connect-timeout", "--noproxy"):
                self.assertIn(option, args)
            self.assertNotIn("--location", args)
            self.assertNotIn("--insecure", args)

    def test_non_https_rejected_before_external_process(self):
        with patch.object(complete.subprocess, "run") as run, self.assertRaises(ValueError):
            complete.fetch_https("http://example.test", 10)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
