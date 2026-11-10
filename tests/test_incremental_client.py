"""Incremental client preparation is exercised without network or app launch."""
import gzip
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import app_updater as updater
import incremental_update as incremental
from tests import test_app_updater as support
from tests.test_incremental_update import VERSION, digest, encode, manifest, record


class IncrementalClientTests(unittest.TestCase):
    # Reuse only the existing disposable storage/signature fixture. Its full
    # installer tests remain in their own class and are not inherited here.
    setUp = support.UpdaterTests.setUp
    metadata = support.UpdaterTests.metadata
    signature = staticmethod(support.UpdaterTests.signature)
    verify_fixture = support.UpdaterTests.verify_fixture

    def configure(self, *, contents=None, document=None):
        contents = contents or {
            "Lineage2ChatTranslator.exe": self.executable.read_bytes(),
            "_internal/unchanged.dll": b"synthetic installed runtime",
            "_internal/changed.txt": b"synthetic new text",
            "_internal/missing.dll": b"synthetic new runtime",
        }
        if document is None:
            document = manifest(*(record(name, data) for name, data in contents.items()))
        self.target = contents
        self.manifest_data = encode(document)
        self.descriptor = {"schema_version": 1, "manifest": {
            "file": f"updates/{VERSION}/files.json", "bytes": len(self.manifest_data),
            "sha256": digest(self.manifest_data)}}
        public = self.metadata()
        public["incremental"] = self.descriptor
        signed = encode(public)
        self.release = updater._signed_release(signed, self.signature(signed))
        self.downloads = {updater.BASE_URL + "/" + self.descriptor["manifest"]["file"]: self.manifest_data,
                          updater.BASE_URL + "/downloads/" + self.release.file: self.binary}
        for entry in document["files"]:
            if entry["path"] in contents:
                self.downloads[updater.BASE_URL + "/updates/blobs/" + entry["blob"]["sha256"] + ".gz"] = \
                    gzip.compress(contents[entry["path"]], mtime=0)
        self.fetches = []
        return document

    def write(self, name, content):
        path = self.executable.parent.joinpath(*name.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def fetch(self, url, operation=None):
        self.fetches.append(url)
        value = self.downloads[url]
        if isinstance(value, BaseException):
            raise value
        return support.Response(value, url=url, length=len(value))

    def prepare(self, *, force_installer=False, progress=None, cancelled=None):
        with patch.object(updater, "_installed_executable", return_value=self.executable), \
             patch.object(updater, "_open", side_effect=self.fetch), \
             patch.object(updater.subprocess, "Popen") as launch:
            prepared = updater.prepare_update(self.release, force_installer=force_installer,
                                              progress=progress, cancelled=cancelled)
        launch.assert_not_called()
        return prepared

    def test_only_changed_or_missing_files_are_downloaded(self):
        document = self.configure()
        self.write("_internal/unchanged.dll", self.target["_internal/unchanged.dll"])
        changed = self.write("_internal/changed.txt", b"synthetic old text")
        prepared = self.prepare()
        expected = {updater.BASE_URL + "/" + self.descriptor["manifest"]["file"]}
        for entry in document["files"]:
            if entry["path"] in {"_internal/changed.txt", "_internal/missing.dll"}:
                expected.add(updater.BASE_URL + "/updates/blobs/" + entry["blob"]["sha256"] + ".gz")
                self.assertEqual((prepared.stage / (entry["sha256"] + ".payload")).read_bytes(),
                                 self.target[entry["path"]])
        self.assertEqual(set(self.fetches), expected)
        self.assertEqual(len(self.fetches), len(expected))
        self.assertFalse((prepared.stage / "installer.exe").exists())
        self.assertEqual((prepared.stage / "files.json").read_bytes(), self.manifest_data)
        self.assertEqual(json.loads((prepared.stage / "job.json").read_text())["mode"], "incremental")
        self.assertEqual(changed.read_bytes(), b"synthetic old text")
        self.assertFalse((self.executable.parent / "_internal/missing.dll").exists())
        updater.discard_update(prepared)
        self.assertFalse(prepared.stage.exists())

    def test_identical_files_share_one_verified_download_and_staged_payload(self):
        document = self.configure(contents={"Lineage2ChatTranslator.exe": self.executable.read_bytes(),
                                           "_internal/one.txt": b"same contents",
                                           "_internal/two.txt": b"same contents"})
        prepared = self.prepare()
        blob = updater.BASE_URL + "/updates/blobs/" + document["files"][1]["blob"]["sha256"] + ".gz"
        self.assertEqual(self.fetches.count(blob), 1)
        self.assertEqual(len(list(prepared.stage.glob("*.payload"))), 1)
        updater.discard_update(prepared)

    def test_matching_inventory_downloads_only_manifest(self):
        self.configure()
        for name, content in self.target.items():
            self.write(name, content)
        prepared = self.prepare()
        self.assertEqual(self.fetches, [updater.BASE_URL + "/" + self.descriptor["manifest"]["file"]])
        self.assertFalse(list(prepared.stage.glob("*.payload")))
        self.assertFalse((prepared.stage / "installer.exe").exists())
        updater.discard_update(prepared)

    def test_force_installer_skips_manifest_and_blobs(self):
        self.configure()
        prepared = self.prepare(force_installer=True)
        self.assertEqual(self.fetches, [updater.BASE_URL + "/downloads/" + self.release.file])
        self.assertEqual((prepared.stage / "installer.exe").read_bytes(), self.binary)
        self.assertFalse((prepared.stage / "files.json").exists())
        updater.discard_update(prepared)

    def test_unavailable_manifest_uses_verified_full_installer(self):
        self.configure()
        url = updater.BASE_URL + "/" + self.descriptor["manifest"]["file"]
        self.downloads[url] = updater.UpdateError("synthetic unavailable file manifest")
        prepared = self.prepare()
        self.assertEqual(self.fetches, [url, updater.BASE_URL + "/downloads/" + self.release.file])
        self.assertEqual((prepared.stage / "installer.exe").read_bytes(), self.binary)
        self.assertFalse(list(prepared.stage.glob("*.payload")))
        updater.discard_update(prepared)

    def test_wrong_manifest_hash_falls_back_without_fetching_untrusted_blob(self):
        self.configure()
        url = updater.BASE_URL + "/" + self.descriptor["manifest"]["file"]
        self.downloads[url] = self.manifest_data.replace(b'"schema_version": 1', b'"schema_version": 2', 1)
        self.assertNotEqual(self.downloads[url], self.manifest_data)
        prepared = self.prepare()
        self.assertEqual(self.fetches, [url, updater.BASE_URL + "/downloads/" + self.release.file])
        self.assertEqual((prepared.stage / "installer.exe").read_bytes(), self.binary)
        updater.discard_update(prepared)

    def test_bad_blob_uses_verified_full_installer_and_discards_partial_payloads(self):
        document = self.configure()
        self.write("_internal/unchanged.dll", self.target["_internal/unchanged.dll"])
        row = document["files"][-1]
        url = updater.BASE_URL + "/updates/blobs/" + row["blob"]["sha256"] + ".gz"
        self.downloads[url] = b"damaged compressed download"
        prepared = self.prepare()
        self.assertIn(url, self.fetches)
        self.assertEqual(self.fetches[-1], updater.BASE_URL + "/downloads/" + self.release.file)
        self.assertEqual((prepared.stage / "installer.exe").read_bytes(), self.binary)
        self.assertFalse(list(prepared.stage.glob("*.payload")))
        self.assertFalse((prepared.stage / "files.json").exists())
        updater.discard_update(prepared)

    def test_fallback_installer_still_requires_its_signed_hash(self):
        self.configure()
        manifest_url = updater.BASE_URL + "/" + self.descriptor["manifest"]["file"]
        self.downloads[manifest_url] = updater.UpdateError("synthetic unavailable manifest")
        self.downloads[updater.BASE_URL + "/downloads/" + self.release.file] = b"MZ damaged installer"
        with self.assertRaises(updater.UpdateError):
            self.prepare()
        cache = self.preferences / "updates"
        self.assertTrue(not cache.exists() or not list(cache.iterdir()))

    def test_cancel_during_incremental_download_never_downloads_full_installer(self):
        self.configure()
        cancel = threading.Event()
        with self.assertRaises(updater.UpdateCancelled):
            self.prepare(progress=lambda *_: cancel.set(), cancelled=cancel)
        self.assertNotIn(updater.BASE_URL + "/downloads/" + self.release.file, self.fetches)
        cache = self.preferences / "updates"
        self.assertTrue(not cache.exists() or not list(cache.iterdir()))

    def test_cancel_during_selection_never_downloads_full_installer(self):
        self.configure()
        cancel = threading.Event()
        original = incremental.changed_files

        def select(*args, **kwargs):
            cancel.set()
            return original(*args, **kwargs)

        with patch.object(incremental, "changed_files", side_effect=select), \
             self.assertRaises(updater.UpdateCancelled):
            self.prepare(cancelled=cancel)
        self.assertNotIn(updater.BASE_URL + "/downloads/" + self.release.file, self.fetches)

    def test_stage_manifest_and_payload_are_reverified_before_helper_launch(self):
        for name in ("files.json", "payload"):
            with self.subTest(name=name):
                self.configure()
                prepared = self.prepare()
                target = prepared.stage / "files.json" if name == "files.json" else next(prepared.stage.glob("*.payload"))
                target.write_bytes(target.read_bytes() + b"changed")
                with patch.object(updater, "_installed_executable", return_value=self.executable), \
                     patch.object(updater.subprocess, "Popen") as launch, self.assertRaises(updater.UpdateError):
                    updater.launch_update(prepared)
                launch.assert_not_called()
                updater.discard_update(prepared)

    def test_mode_change_cannot_run_a_missing_or_unverified_installer(self):
        self.configure()
        prepared = self.prepare()
        job_file = prepared.stage / "job.json"
        job = json.loads(job_file.read_text())
        job["mode"] = "installer"
        job_file.write_text(json.dumps(job))
        with patch.object(updater, "_installed_executable", return_value=self.executable), \
             patch.object(updater.subprocess, "Popen") as launch, self.assertRaises((updater.UpdateError, OSError)):
            updater.launch_update(prepared)
        launch.assert_not_called()
        updater.discard_update(prepared)

    def test_fixed_origin_rules_include_only_canonical_manifest_and_blob_routes(self):
        bad = ("/updates/../private", "/updates/blobs/not-a-hash.gz", "/updates/blobs/" + "a" * 64 + ".gz?x=1",
               "/updates/99.8.7/files.json/extra", "/updates/99.8.7/files.json?x=1")
        for path in bad:
            with self.subTest(path=path), patch.object(updater, "_opener") as opener, \
                 self.assertRaises(updater.UpdateError):
                updater._open(updater.BASE_URL + path)
            opener.assert_not_called()

    def test_active_or_failed_rollback_journal_preserves_every_backup(self):
        for state in ("applying", "rollback_failed", "unknown"):
            with self.subTest(state=state):
                self.configure()
                prepared = self.prepare()
                backup = prepared.stage / "rollback-00000.bak"
                backup.write_bytes(b"synthetic original application backup")
                journal = prepared.stage / "transaction.json"
                journal.write_text(json.dumps({"state": state}))
                with self.assertRaises(updater.UpdateError):
                    updater.discard_update(prepared)
                updater.cleanup_update_cache(strict=False)
                self.assertEqual(backup.read_bytes(), b"synthetic original application backup")
                self.assertTrue((prepared.stage / "owner.json").exists())
                with self.assertRaises(updater.UpdateError):
                    updater.ensure_no_interrupted_update()
                journal.write_text(json.dumps({"state": "rolled_back"}))
                updater.discard_update(prepared)

    def test_unreadable_journal_preserves_backups_and_blocks_normal_startup(self):
        self.configure()
        prepared = self.prepare()
        backup = prepared.stage / "rollback-00000.bak"
        backup.write_bytes(b"synthetic backup")
        journal = prepared.stage / "transaction.json"
        journal.write_bytes(b"{incomplete JSON")
        updater.cleanup_update_cache(strict=False)
        self.assertEqual(backup.read_bytes(), b"synthetic backup")
        with self.assertRaises(updater.UpdateError):
            updater.ensure_no_interrupted_update()
        journal.write_text(json.dumps({"state": "rolled_back"}))
        updater.discard_update(prepared)

    def test_pending_journal_preserves_backups_even_with_terminal_previous_journal(self):
        self.configure()
        prepared = self.prepare()
        backup = prepared.stage / "rollback-00000.bak"
        backup.write_bytes(b"synthetic backup")
        (prepared.stage / "transaction.json").write_text(json.dumps({"state": "committed"}))
        pending = prepared.stage / "transaction.pending"
        pending.write_bytes(b"incomplete next journal")
        updater.cleanup_update_cache(strict=False)
        self.assertEqual(backup.read_bytes(), b"synthetic backup")
        with self.assertRaises(updater.UpdateError):
            updater.ensure_no_interrupted_update()
        pending.unlink()
        updater.discard_update(prepared)

    def test_terminal_journal_allows_only_owned_recovery_files_to_be_cleaned(self):
        for state in ("committed", "rolled_back"):
            with self.subTest(state=state):
                self.configure()
                prepared = self.prepare()
                (prepared.stage / "rollback-00000.bak").write_bytes(b"synthetic backup")
                (prepared.stage / "transaction.json").write_text(json.dumps({"state": state}))
                updater.ensure_no_interrupted_update()
                updater.discard_update(prepared)
                self.assertFalse(prepared.stage.exists())


if __name__ == "__main__":
    unittest.main()
