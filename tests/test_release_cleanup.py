"""Isolated release-retention and native deletion regression cases."""
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("release_cleanup_tested", Path(__file__).resolve().parents[1] / "packaging/release_cleanup.py")
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


class CleanupFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="l2ct-release-cleanup-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "workspace"
        self.latest = self.root / ".build/1.7.13-20261109T155906Z"
        self.latest.mkdir(parents=True)
        (self.root / "release").mkdir()
        self.write(self.root / ".build/latest-build.json", json.dumps({"build": str(self.latest)}))
        self.old = self.root / ".build/1.7.12-20261109T143924Z"
        self.write(self.old / "dist/app.exe", "disposable old build")
        self.keep = [self.latest / "dist/app.exe", self.root / "translator_core.py",
                     self.root / "release/1.7.12/Lineage-2-Chat-Translator-1.7.12-Setup.exe",
                     self.root / "release/1.7.12/release-manifest.json",
                     self.root / "sites/l2-chat-translator/dist/downloads/older.exe",
                     self.root / "public-source/repository/source.py",
                     self.root / ".build-tools/venv/python.exe",
                     self.root / ".build-tools/inno-portable/iscc.exe",
                     self.root / ".build-tools/secure-ollama/output/ollama.exe",
                     self.root / ".build/source-publication.local.json",
                     self.root / "models/blobs/current-model"]
        for path in self.keep:
            self.write(path, "keep " + path.name)
        self.old_time = datetime(2026, 11, 9, 14, tzinfo=timezone.utc).timestamp()
        self.new_time = datetime(2026, 11, 9, 17, tzinfo=timezone.utc).timestamp()
        os.utime(self.root / ".build-tools/secure-ollama/output/ollama.exe", (self.old_time, self.old_time))

    def write(self, path, content="disposable"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def archive(self, path, *, new=False):
        self.write(path)
        stamp = self.new_time if new else self.old_time
        os.utime(path, (stamp, stamp))
        return path

    def plan(self):
        return cleanup.plan_cleanup(self.root, "1.7.13", self.latest)


class CleanupPlanTests(CleanupFixture):
    def test_preserves_source_models_release_history_tools_and_latest_build(self):
        paths = {self.root / row["path"] for row in self.plan()}
        self.assertEqual(paths, {self.old})
        for keep in self.keep:
            self.assertFalse(any(keep == path or keep.is_relative_to(path) for path in paths))

    def test_old_known_packages_and_backend_cache_selected_only(self):
        expected = [self.archive(self.root / "release/Lineage-2-Chat-Translator-1.7.12-main-server.zip"),
                    self.archive(self.root / ".build/site-1.7.12-unified-release.tar.gz"),
                    self.archive(self.root / ".build/release-1712-site.tar"),
                    self.root / ".build-tools/secure-ollama/gocache"]
        self.write(expected[-1] / "cache.bin")
        self.archive(self.root / ".build/personal-backup.zip")
        self.archive(self.root / "release/personal-main-server.zip")
        self.assertEqual({self.root / row["path"] for row in self.plan()}, {self.old, *expected})

    def test_future_builds_and_newer_same_version_work_are_preserved(self):
        self.write(self.root / ".build/1.7.10-20261109T120000Z/unpublished.py")
        self.write(self.root / ".build/1.7.14-20261109T150000Z/work.py")
        self.write(self.root / ".build/1.7.13-20261109T170000Z/work.py")
        self.archive(self.root / "release/Lineage-2-Chat-Translator-1.7.14-main-server.zip")
        self.archive(self.root / "release/Lineage-2-Chat-Translator-1.7.13-main-server.zip", new=True)
        self.archive(self.root / ".build/release-1.7.13-site.tar", new=True)
        self.assertEqual({row["path"] for row in self.plan()}, {self.old.relative_to(self.root).as_posix()})

    def test_newer_backend_output_preserves_its_unpublished_build_cache(self):
        self.write(self.root / ".build-tools/secure-ollama/gocache/keep.bin")
        os.utime(self.root / ".build-tools/secure-ollama/output/ollama.exe", (self.new_time, self.new_time))
        self.assertEqual({row["path"] for row in self.plan()}, {self.old.relative_to(self.root).as_posix()})

    def test_latest_pointer_argument_must_be_inside_current_release_build(self):
        for latest in (self.root.parent, self.old, self.root / "release/1.7.13", self.root / ".build/1.7.13-20261309T155906Z"):
            with self.subTest(latest=latest), self.assertRaises((cleanup.CleanupError, OSError)):
                cleanup.plan_cleanup(self.root, "1.7.13", latest)

    def test_unknown_dotted_or_compact_archive_names_stay(self):
        self.archive(self.root / ".build/release-1711-site.tar")  # No canonical release identifies token.
        self.archive(self.root / ".build/site-1.7.13-secrets.zip")
        self.assertEqual({row["path"] for row in self.plan()}, {self.old.relative_to(self.root).as_posix()})


@unittest.skipUnless(os.name == "nt", "Native Windows handle deletion")
class NativeCleanupTests(CleanupFixture):
    def test_removes_only_disposable_files_and_is_idempotent(self):
        archive = self.archive(self.root / "release/Lineage-2-Chat-Translator-1.7.12-hosting.zip")
        cache = self.write(self.root / ".build-tools/secure-ollama/gopath/readonly.txt")
        os.chmod(cache, 0o444)
        expected = {str(p): p.read_bytes() for p in self.keep}
        report = cleanup.cleanup_release(self.root, "1.7.13", self.latest)
        self.assertEqual(report["removed_targets"], 3)
        self.assertGreater(report["removed_bytes"], 0)
        self.assertFalse(self.old.exists())
        self.assertFalse(archive.exists())
        self.assertFalse(cache.exists())
        self.assertEqual({str(p): p.read_bytes() for p in self.keep}, expected)
        self.assertEqual(cleanup.cleanup_release(self.root, "1.7.13", self.latest)["removed_targets"], 0)

    def test_all_candidates_preflight_before_any_deletion(self):
        earlier = self.write(self.root / ".build/1.7.11-20261109T130000Z/keep-until-all-pass.txt")
        self.write(self.root / "release/1.7.11/release-manifest.json")
        self.write(self.root / "release/1.7.11/Lineage-2-Chat-Translator-1.7.11-Setup.exe")
        outside = self.write(self.root.parent / "personal.txt", "do not touch")
        os.link(outside, self.old / "linked.txt")
        with self.assertRaisesRegex(cleanup.CleanupError, "hard-linked"):
            cleanup.cleanup_release(self.root, "1.7.13", self.latest)
        self.assertTrue(earlier.exists())
        self.assertTrue((self.old / "dist/app.exe").exists())
        self.assertEqual(outside.read_text(), "do not touch")

    def test_rejects_model_storage_nested_inside_generated_build_or_backend_cache(self):
        for target in (self.old / "models/blobs/model", self.root / ".build-tools/secure-ollama/work/models/blobs/model"):
            with self.subTest(target=target):
                self.write(target, "model must remain")
                with self.assertRaisesRegex(cleanup.CleanupError, "model storage"):
                    cleanup.cleanup_release(self.root, "1.7.13", self.latest)
                self.assertEqual(target.read_text(), "model must remain")
                target.unlink()
                target.parent.rmdir()
                target.parent.parent.rmdir()

    def test_changed_latest_pointer_stops_before_deleting_old_build(self):
        self.write(self.root / ".build/latest-build.json", json.dumps({"build": str(self.old)}))
        with self.assertRaisesRegex(cleanup.CleanupError, "Latest build changed"):
            cleanup.cleanup_release(self.root, "1.7.13", self.latest)
        self.assertTrue((self.old / "dist/app.exe").exists())

    def test_installation_marker_preserves_candidate_even_with_mixed_case_name(self):
        marker = self.write(self.old / ".L2-Translator-Install.JSON", "user installation")
        with self.assertRaisesRegex(cleanup.CleanupError, "model storage"):
            cleanup.cleanup_release(self.root, "1.7.13", self.latest)
        self.assertTrue(marker.exists())
        self.assertTrue((self.old / "dist/app.exe").exists())

    def test_pinned_target_cannot_be_replaced_during_deletion(self):
        make_deletable = cleanup._CleanupWindows.make_deletable
        attempts = []
        def attempt(api, handle):
            if not attempts:
                try:
                    self.old.rename(self.root / ".build/moved")
                except OSError:
                    attempts.append("blocked")
                else:
                    attempts.append("moved")
            return make_deletable(api, handle)
        with patch.object(cleanup._CleanupWindows, "make_deletable", attempt):
            cleanup.cleanup_release(self.root, "1.7.13", self.latest)
        self.assertEqual(attempts, ["blocked"])

    def test_partial_failure_records_completed_targets_and_preserves_remaining(self):
        archive = self.archive(self.root / "release/Lineage-2-Chat-Translator-1.7.12-hosting.zip")
        inspect = cleanup._CleanupWindows.inspect
        make_deletable = cleanup._CleanupWindows.make_deletable
        archive_handles = set()
        def remember(api, handle, path):
            if path == archive:
                archive_handles.add(handle)
            return inspect(api, handle, path)
        def fail_archive(api, handle):
            if handle in archive_handles:
                raise PermissionError("synthetic archive deletion failure")
            return make_deletable(api, handle)
        with patch.object(cleanup._CleanupWindows, "inspect", remember), \
             patch.object(cleanup._CleanupWindows, "make_deletable", fail_archive), \
             self.assertRaisesRegex(PermissionError, "synthetic archive"):
            cleanup.cleanup_release(self.root, "1.7.13", self.latest)
        self.assertFalse(self.old.exists())
        self.assertTrue(archive.exists())
        progress = json.loads((self.root / ".build/release-cleanup-1.7.13.local.json").read_text())
        self.assertEqual(progress["status"], "partial_failure")
        self.assertEqual(progress["removed_targets"], 1)
        self.assertEqual(cleanup.cleanup_release(self.root, "1.7.13", self.latest)["removed_targets"], 1)

    def test_symbolic_link_candidate_is_never_followed(self):
        outside = self.root.parent / "personal-directory"
        outside.mkdir()
        self.write(outside / "keep.txt", "keep")
        link = self.root / ".build/1.7.10-20261109T120000Z"
        self.write(self.root / "release/1.7.10/release-manifest.json")
        self.write(self.root / "release/1.7.10/Lineage-2-Chat-Translator-1.7.10-Setup.exe")
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Windows does not permit test symlink creation")
        with self.assertRaises((cleanup.CleanupError, OSError)):
            cleanup.cleanup_release(self.root, "1.7.13", self.latest)
        self.assertEqual((outside / "keep.txt").read_text(), "keep")
        self.assertTrue((self.old / "dist/app.exe").exists())
