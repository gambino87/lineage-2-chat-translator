"""Source publication safeguards with temporary local Git repos; no remote service."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("l2_source_release", PROJECT / "packaging/source_release.py")
source = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(source)


class SnapshotFixture:
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = self.base / "private-workspace"
        self.root.mkdir()
        self.put("app.py", "VALUE = 1\n")
        self.put("assets/item.json", '{"item": "Example sword"}\n')
        self.put("notes.md", "Synthetic public notes.\n")
        self.put("packaging/build.py", 'SOURCES = ("app_version.py", "app.py")\nASSETS = ("assets/item.json",)\n')
        names = {"packaging/build.py", "notes.md", source.CONFIG}
        for filename in source.TEMPLATES.values():
            name = "packaging/public-source/" + filename
            self.put(name, (PROJECT / name).read_bytes())
            names.add(name)
        self.put(source.CONFIG, json.dumps(sorted(names)))
        self.release()

    def put(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value if isinstance(value, bytes) else value.encode())

    def release(self, version="1.2.3"):
        self.put("app_version.py", f'VERSION = "{version}"\n')
        installer = b"synthetic installer " + version.encode()
        name = f"Lineage-2-Chat-Translator-{version}-Setup.exe"
        artifact = {"file": name, "bytes": len(installer), "sha256": source.sha256(installer)}
        manifest = {"product": "Lineage 2 Chat Translator", "platform": "windows-x64", "version": version,
                    "packaged_self_test": {"ok": True, "frozen": True, "version": version},
                    "sources": [{"path": path, "sha256": source.sha256((self.root / path).read_bytes())}
                                for path in ("app_version.py", "app.py")],
                    "assets": [{"path": "assets/item.json", "sha256": source.sha256((self.root / "assets/item.json").read_bytes())}],
                    "installer": artifact, "toolchain": {"python": "3.12.10"}}
        self.put(f"release/{version}/{name}", installer)
        self.put(f"release/{version}/release-manifest.json", json.dumps(manifest))
        self.put(f"release/{version}/SHA256SUMS.txt", f"{artifact['sha256']}  {name}\n")
        return manifest

    def include(self, name):
        path = self.root / source.CONFIG
        names = json.loads(path.read_text())
        names.append(name)
        self.put(source.CONFIG, json.dumps(names))


class SnapshotTests(SnapshotFixture, unittest.TestCase):

    def test_snapshot_excludes_private_and_generated_files(self):
        for name in ("settings.json", "review_tools/old.py", "private.local.json", "AGENTS.md",
                     "release/old.exe", "sites/nested/.git/config"):
            self.put(name, "PRIVATE SENTINEL")
        files, manifest = source.snapshot(self.root)
        self.assertNotIn(b"PRIVATE SENTINEL", b"".join(files.values()))
        self.assertEqual(manifest["version"], "1.2.3")
        self.assertIn(b"v1.2.3", files["README.md"])
        self.assertNotIn(b"740 tests", files["README.md"])
        self.assertEqual({row["path"] for row in manifest["files"]}, set(files) - {source.MANIFEST})

    def test_changed_app_or_installer_is_rejected(self):
        self.put("app.py", "VALUE = 2\n")
        with self.assertRaisesRegex(ValueError, "Application input changed"):
            source.snapshot(self.root)
        self.release()
        self.put("release/1.2.3/Lineage-2-Chat-Translator-1.2.3-Setup.exe", b"tampered")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            source.snapshot(self.root)

    def test_unrecognized_release_metadata_is_not_published(self):
        for field in ("installer", "sources"):
            manifest = self.release()
            target = manifest[field][0] if field == "sources" else manifest[field]
            target["private_note"] = "PRIVATE SENTINEL"
            self.put("release/1.2.3/release-manifest.json", json.dumps(manifest))
            with self.assertRaises(ValueError):
                source.snapshot(self.root)

    def test_credential_marker_failure_does_not_echo_content(self):
        fake = "gh" + "p_" + "Z" * 36
        self.put("notes.md", fake)
        with self.assertRaises(ValueError) as caught:
            source.snapshot(self.root)
        self.assertNotIn(fake, str(caught.exception))
        self.assertIn("notes.md", str(caught.exception))

    def test_forbidden_allowlist_paths_and_duplicates_fail(self):
        original = (self.root / source.CONFIG).read_bytes()
        for name in ("../escape.py", "settings.json", "private.local.json", "library.dll", "notes.md"):
            self.put(source.CONFIG, original)
            self.include(name)
            with self.assertRaises(ValueError):
                source.snapshot(self.root)

    def test_known_identifying_fixtures_are_deidentified(self):
        alias = "J" + "OACO"
        self.put("tests/test_sample.py", f'NAME = "{alias}"\n')
        self.include("tests/test_sample.py")
        files, _ = source.snapshot(self.root)
        self.assertIn(b"ExamplePlayer", files["tests/test_sample.py"])
        self.assertNotIn(alias.encode(), files["tests/test_sample.py"])

    def test_new_build_helper_requires_explicit_allowlist_review(self):
        self.put("packaging/new_helper.py", "VALUE = 1\n")
        with self.assertRaisesRegex(ValueError, "allowlist review"):
            source.snapshot(self.root)
        self.include("packaging/new_helper.py")
        self.assertIn("packaging/new_helper.py", source.snapshot(self.root)[0])

    def test_ancestor_link_is_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "sample.py").write_text("VALUE = 1\n")
        try:
            (self.root / "linked").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Creating directory symlinks is unavailable.")
        self.include("linked/sample.py")
        with self.assertRaisesRegex(ValueError, "Linked"):
            source.snapshot(self.root)

    def test_receipt_detects_changes_after_publication(self):
        files, manifest = source.snapshot(self.root)
        receipt = {"repository": source.REPOSITORY, "public_verified": True, "version": "1.2.3",
                   "installer": {key: manifest["published_installer"][key] for key in ("file", "bytes", "sha256")},
                   "snapshot_sha256": source.snapshot_digest(files)}
        source.verify_source_current(self.root, receipt)
        self.put("notes.md", "Changed after source publication.\n")
        with self.assertRaisesRegex(ValueError, "no longer matches"):
            source.verify_source_current(self.root, receipt)


@unittest.skipUnless(shutil.which("git"), "Git is required for local publication regression tests.")
class GitPublicationTests(SnapshotFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.bare = self.base / "remote.git"
        self.git(self.base, "init", "--bare", "-b", "main", str(self.bare))
        self.repo = self.root / source.CLONE
        self.repo.mkdir(parents=True)
        self.git(self.repo, "init", "-b", "main")
        self.git(self.repo, "config", "core.autocrlf", "false")
        self.git(self.repo, "remote", "add", "origin", self.bare.as_posix())
        self.write_snapshot()
        self.git(self.repo, "add", "--all")
        self.git(self.repo, "commit", "-m", "initial synthetic source")
        self.git(self.repo, "push", "-u", "origin", "main")
        self.enterContext(patch.object(source, "REMOTE", self.bare.as_posix()))
        self.public = self.enterContext(patch.object(source, "_verify_public"))

    def git(self, repo, *args):
        result = subprocess.run(["git", "-c", "user.name=Example", "-c", "user.email=example@example.invalid",
                                 "-c", "commit.gpgsign=false", "-c", "tag.gpgSign=false", *args],
                                cwd=repo, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, "Local synthetic Git operation failed: " + " ".join(args))
        return result.stdout.strip()

    def write_snapshot(self):
        files, _ = source.snapshot(self.root)
        for name, content in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    def test_publication_is_idempotent_and_keeps_existing_tag_for_docs_update(self):
        first = source.publish_source(self.root)
        tag = self.git(self.repo, "rev-parse", "v1.2.3^{commit}")
        self.assertEqual(source.publish_source(self.root)["commit"], first["commit"])
        self.put("notes.md", "New public documentation.\n")
        newer = source.publish_source(self.root)
        self.assertNotEqual(newer["commit"], first["commit"])
        self.assertEqual(self.git(self.repo, "rev-parse", "v1.2.3^{commit}"), tag)
        self.assertEqual(self.git(self.bare, "rev-parse", "main"), newer["commit"])
        source.verify_source_current(self.root, newer)

    def test_new_application_version_gets_new_matching_tag(self):
        source.publish_source(self.root)
        self.release("1.2.4")
        result = source.publish_source(self.root)
        self.assertEqual(result["version"], "1.2.4")
        self.assertEqual(self.git(self.bare, "rev-parse", "v1.2.4^{commit}"), result["commit"])

    def test_same_version_changed_application_identity_is_rejected(self):
        source.publish_source(self.root)
        before = self.git(self.bare, "rev-parse", "main")
        self.put("app.py", "VALUE = 2\n")
        self.release()
        with self.assertRaisesRegex(ValueError, "tags are immutable"):
            source.publish_source(self.root)
        self.assertEqual(self.git(self.bare, "rev-parse", "main"), before)

    def test_dirty_clone_and_wrong_remote_are_rejected(self):
        (self.repo / "notes.md").write_text("Unrelated user edit.\n")
        with self.assertRaisesRegex(ValueError, "local changes"):
            source.publish_source(self.root)
        self.git(self.repo, "restore", "notes.md")
        self.git(self.repo, "remote", "set-url", "origin", (self.base / "wrong.git").as_posix())
        with self.assertRaisesRegex(ValueError, "remote"):
            source.publish_source(self.root)

    def test_failed_push_can_retry_own_commit_without_duplicating_it(self):
        self.put("notes.md", "New version of the source notes.\n")
        original = source._git
        def fail_push(repo, *args, **kwargs):
            if args[0] == "push":
                raise ValueError("Synthetic push failure")
            return original(repo, *args, **kwargs)
        with patch.object(source, "_git", side_effect=fail_push):
            with self.assertRaisesRegex(ValueError, "push failure"):
                source.publish_source(self.root)
        pending = self.git(self.repo, "rev-parse", "HEAD")
        result = source.publish_source(self.root)
        self.assertEqual(result["commit"], pending)
        self.assertEqual(self.git(self.bare, "rev-parse", "main"), pending)

    def test_unrelated_local_commit_is_never_pushed(self):
        (self.repo / "notes.md").write_text("Unrelated source edits.\n")
        self.git(self.repo, "add", "notes.md")
        self.git(self.repo, "commit", "-m", "unrelated")
        before = self.git(self.bare, "rev-parse", "main")
        with self.assertRaises(ValueError):
            source.publish_source(self.root)
        self.assertEqual(self.git(self.bare, "rev-parse", "main"), before)

    def test_unpublished_tag_cannot_publish_an_unaudited_commit(self):
        self.git(self.repo, "tag", "-a", "v1.2.3", "-m", "synthetic local tag")
        self.put("notes.md", "A changed candidate.\n")
        with self.assertRaisesRegex(ValueError, "Unpublished local version tag"):
            source.publish_source(self.root)
        self.assertEqual(self.git(self.bare, "tag", "--list"), "")

    def test_unpublished_annotated_tag_requires_own_retry_record(self):
        self.git(self.repo, "tag", "-a", "v1.2.3", "-m", "unreviewed annotation")
        with self.assertRaisesRegex(ValueError, "not a recorded publication retry"):
            source.publish_source(self.root)
        self.assertEqual(self.git(self.bare, "tag", "--list"), "")

    def test_index_bytes_must_match_scanned_worktree_bytes(self):
        altered = self.repo / "altered-content.tmp"
        altered.write_bytes(b"UNSCANNED CONTENT")
        blob = self.git(self.repo, "hash-object", "-w", str(altered))
        self.git(self.repo, "update-index", "--cacheinfo", "100644", blob, "notes.md")
        with self.assertRaisesRegex(ValueError, "index bytes differ"):
            source._tracked_files(self.repo)

    def test_gitfile_is_rejected_before_git_or_network_work(self):
        metadata = self.repo / ".git"
        metadata.rename(self.repo / "saved-metadata")
        metadata.write_text("gitdir: saved-metadata\n")
        with self.assertRaisesRegex(ValueError, "independent"):
            source.publish_source(self.root)
        self.public.assert_not_called()


if __name__ == "__main__":
    unittest.main()
