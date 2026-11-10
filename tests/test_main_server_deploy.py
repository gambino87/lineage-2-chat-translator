"""Static deployment validation in temporary folders; no server or service access."""
import hashlib
import gzip
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile


_SPEC = importlib.util.spec_from_file_location(
    "l2_main_server_deploy", Path(__file__).resolve().parents[1] / "packaging/main-server/deploy_release.py"
)
deploy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(deploy)


class MainServerDeployTests(unittest.TestCase):
    def setUp(self):
        self.temporary = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = self.temporary / "server"
        self.sequence = 0

    def require_directory_symlinks(self):
        target = self.temporary / "symlink-probe-target"
        link = self.temporary / "symlink-probe"
        target.mkdir(exist_ok=True)
        try:
            link.symlink_to(target, target_is_directory=True)
            supported = link.is_dir()
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Directory symlink creation is unavailable: {exc}")
        finally:
            if link.is_symlink():
                link.unlink()
        if not supported:
            self.skipTest("Directory symlink resolution is unavailable")

    @staticmethod
    def installer_name(version):
        return f"Lineage-2-Chat-Translator-{version}-Setup.exe"

    def public_files(self, version="1.2.3", content=b"synthetic installer", old=None):
        name = self.installer_name(version)
        artifact = {"file": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        files = {
            "index.html": f'<html><a href="/downloads/{name}">Download v{version}</a></html>'.encode(),
            "_headers": b"/*\n  X-Content-Type-Options: nosniff\n",
            "robots.txt": b"User-agent: *\nAllow: /\n",
            "assets/L2CT.ico": b"synthetic ico",
            "assets/L2CT.png": b"synthetic png",
            "downloads/release.json": json.dumps({"version": version, "installer": artifact}).encode(),
            "downloads/release.sig": b"s" * 384,
            "downloads/" + name: content,
        }
        files.update(old or {})
        files["downloads/SHA256SUMS.txt"] = "".join(
            f"{hashlib.sha256(data).hexdigest()}  {Path(path).name}\n"
            for path, data in sorted(files.items()) if path.endswith(".exe")
        ).encode()
        return files, artifact

    @staticmethod
    def add_updates(files, *, corrupt_raw_hash=False, orphan=False):
        public = json.loads(files["downloads/release.json"])
        version = public["version"]
        raw = b"MZ synthetic updated application"
        compressed = gzip.compress(raw, mtime=0)
        blob = {"bytes": len(compressed), "sha256": hashlib.sha256(compressed).hexdigest()}
        row = {"path": "Lineage2ChatTranslator.exe", "bytes": len(raw),
               "sha256": "0" * 64 if corrupt_raw_hash else hashlib.sha256(raw).hexdigest(), "blob": blob}
        cleanup = {"schema_version": 1, "product": "Lineage 2 Chat Translator", "version": version,
                   "current_paths": [row["path"]], "obsolete_files": []}
        document = {"schema_version": 1, "product": "Lineage 2 Chat Translator", "platform": "windows-x64",
                    "version": version, "files": [row], "cleanup": cleanup}
        data = json.dumps(document).encode()
        name = "updates/" + version + "/files.json"
        public["incremental"] = {"schema_version": 1, "manifest": {"file": name, "bytes": len(data),
                                   "sha256": hashlib.sha256(data).hexdigest()}}
        files["downloads/release.json"] = json.dumps(public).encode()
        files[name] = data
        files["updates/blobs/" + blob["sha256"] + ".gz"] = compressed
        if orphan:
            files["updates/blobs/" + "0" * 64 + ".gz"] = b"unreferenced"

    def test_incremental_bundle_audits_both_compressed_and_installed_bytes(self):
        archive, manifest, _ = self.bundle(mutate=self.add_updates)
        self.assertEqual(deploy.verify_bundle(archive, manifest), manifest["files"])
        archive, manifest, _ = self.bundle(mutate=lambda files: self.add_updates(files, corrupt_raw_hash=True))
        self.assert_rejected_before_activation(archive, manifest, "Expanded update hash/size mismatch")

    def test_incremental_orphans_missing_objects_and_changed_manifest_are_rejected(self):
        archive, manifest, _ = self.bundle(mutate=lambda files: self.add_updates(files, orphan=True))
        self.assert_rejected_before_activation(archive, manifest, "Unreferenced")
        for mutation, error in ((lambda files: files.pop(next(name for name in files if name.startswith("updates/blobs/"))), "Missing or incorrect"),
                                (lambda files: files.update({"updates/1.2.3/files.json": b"changed"}), "differs from signed")):
            def mutate(files):
                self.add_updates(files)
                mutation(files)
            archive, manifest, _ = self.bundle(mutate=mutate)
            self.assert_rejected_before_activation(archive, manifest, error)

    def bundle(self, version="1.2.3", content=b"synthetic installer", old=None,
               mutate=None, file_mode=None):
        self.sequence += 1
        folder = self.temporary / f"bundle-{self.sequence}"
        folder.mkdir()
        archive = folder / f"Lineage-2-Chat-Translator-{version}-main-server.zip"
        files, artifact = self.public_files(version, content, old)
        if mutate:
            mutate(files)
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for path, data in sorted(files.items()):
                info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (file_mode.get(path, stat.S_IFREG | 0o644)
                                      if file_mode else stat.S_IFREG | 0o644) << 16
                output.writestr(info, data)
        manifest = {
            "schema_version": 1, "version": version, "base_url": deploy.BASE_URL,
            "installer": artifact,
            "files": [{"path": path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                      for path, data in sorted(files.items())],
        }
        self.reidentify(manifest)
        manifest["archive"] = {"file": archive.name, "bytes": archive.stat().st_size,
                               "sha256": deploy.digest(archive)}
        return archive, manifest, files

    @staticmethod
    def reidentify(manifest):
        identity = {key: manifest[key] for key in ("schema_version", "version", "base_url", "installer", "files")}
        data = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        manifest["release_id"] = manifest["version"] + "-" + hashlib.sha256(data).hexdigest()[:12]

    def assert_rejected_before_activation(self, archive, manifest, expected=None):
        # Even a current pointer that would fail later validation must remain untouched.
        self.root.mkdir(exist_ok=True)
        current = self.root / "current"
        current.write_bytes(b"existing deployment marker")
        before = {path.relative_to(self.root).as_posix(): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        context = self.assertRaisesRegex((ValueError, KeyError, TypeError, zipfile.BadZipFile), expected) if expected else self.assertRaises((ValueError, KeyError, TypeError, zipfile.BadZipFile))
        with context:
            deploy.publish(archive, manifest, self.root)
        after = {path.relative_to(self.root).as_posix(): path.read_bytes()
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertFalse((self.root / "releases").exists())
        self.assertFalse((self.root / "manifests").exists())

    def test_tampered_archive_is_rejected_before_activation(self):
        archive, manifest, _ = self.bundle()
        archive.write_bytes(archive.read_bytes() + b"tampered")
        self.assert_rejected_before_activation(archive, manifest, "Archive verification failed")

    def test_malformed_zip_is_rejected_even_with_matching_outer_checksum(self):
        archive, manifest, _ = self.bundle()
        archive.write_bytes(b"not a ZIP archive")
        manifest["archive"].update(bytes=archive.stat().st_size, sha256=deploy.digest(archive))
        self.assert_rejected_before_activation(archive, manifest)

    def test_nonpublic_and_traversal_files_are_rejected(self):
        for unsafe in ("settings.json", "../outside", "/absolute", "downloads/secret.key", "assets/../../escape"):
            with self.subTest(path=unsafe):
                archive, manifest, _ = self.bundle(mutate=lambda files: files.update({unsafe: b"private"}))
                self.assert_rejected_before_activation(archive, manifest, "Non-public file")

    def test_missing_public_file_is_rejected(self):
        archive, manifest, _ = self.bundle(mutate=lambda files: files.pop("robots.txt"))
        self.assert_rejected_before_activation(archive, manifest, "Missing or duplicate")

    def test_duplicate_manifest_paths_are_rejected(self):
        archive, manifest, _ = self.bundle()
        manifest["files"].append(dict(manifest["files"][0]))
        self.reidentify(manifest)
        self.assert_rejected_before_activation(archive, manifest, "Missing or duplicate")

    def test_nonregular_archive_member_is_rejected(self):
        archive, manifest, _ = self.bundle(file_mode={"index.html": stat.S_IFLNK | 0o777})
        self.assert_rejected_before_activation(archive, manifest, "Non-regular ZIP member")

    def test_wrong_member_checksum_is_rejected_even_with_valid_manifest_identity(self):
        archive, manifest, _ = self.bundle()
        manifest["files"][0]["sha256"] = "0" * 64
        self.reidentify(manifest)
        self.assert_rejected_before_activation(archive, manifest, "Incorrect ZIP content")

    def test_manifest_wrong_destination_or_identity_is_rejected(self):
        archive, original, _ = self.bundle()
        for changes in ({"base_url": "https://wrong.example"}, {"schema_version": 2},
                        {"release_id": "1.2.3-invalid"}, {"version": "../../outside"}):
            with self.subTest(changes=changes):
                manifest = json.loads(json.dumps(original))
                manifest.update(changes)
                self.assert_rejected_before_activation(archive, manifest)

    def test_public_metadata_must_match_current_version_and_installer(self):
        cases = ({"version": "1.2.2"}, {"file": "wrong.exe"},
                 {"bytes": 1}, {"sha256": "0" * 64})
        for changes in cases:
            with self.subTest(changes=changes):
                def mutate(files):
                    public = json.loads(files["downloads/release.json"])
                    if "version" in changes:
                        public.update(changes)
                    else:
                        public["installer"].update(changes)
                    files["downloads/release.json"] = json.dumps(public).encode()

                archive, manifest, _ = self.bundle(mutate=mutate)
                self.assert_rejected_before_activation(archive, manifest, "Public release metadata differs")

    def test_unsigned_or_wrong_length_signature_is_rejected_before_activation(self):
        for signature in (None, b"short", b"x" * 385):
            with self.subTest(signature_size=None if signature is None else len(signature)):
                def mutate(files):
                    if signature is None:
                        del files["downloads/release.sig"]
                    else:
                        files["downloads/release.sig"] = signature
                archive, manifest, _ = self.bundle(mutate=mutate)
                self.assert_rejected_before_activation(archive, manifest)

    def test_public_checksums_must_cover_every_installer_with_exact_hashes(self):
        old_name = self.installer_name("1.2.2")
        old = {"downloads/" + old_name: b"old synthetic installer"}
        for case in ("wrong_current_hash", "wrong_old_hash", "missing_old", "extra", "duplicate", "malformed"):
            with self.subTest(case=case):
                def mutate(files):
                    lines = files["downloads/SHA256SUMS.txt"].decode().splitlines()
                    if case in ("wrong_current_hash", "wrong_old_hash"):
                        target = old_name if case == "wrong_old_hash" else self.installer_name("1.2.3")
                        lines = ["0" * 64 + "  " + target if line.endswith("  " + target) else line
                                 for line in lines]
                    elif case == "missing_old":
                        lines = [line for line in lines if not line.endswith("  " + old_name)]
                    elif case == "extra":
                        lines.append("0" * 64 + "  " + self.installer_name("0.0.1"))
                    elif case == "duplicate":
                        lines.append(lines[0])
                    else:
                        lines[0] = "malformed checksum"
                    files["downloads/SHA256SUMS.txt"] = ("\n".join(lines) + "\n").encode()

                archive, manifest, _ = self.bundle(old=old, mutate=mutate)
                self.assert_rejected_before_activation(archive, manifest, "[Cc]hecksums")

    def test_successful_release_switch_is_atomic_and_same_release_is_idempotent(self):
        self.require_directory_symlinks()
        first_archive, first_manifest, first_files = self.bundle()
        first = deploy.publish(first_archive, first_manifest, self.root)
        current = self.root / "current"
        old_release = current.resolve()
        old_name = "downloads/" + self.installer_name("1.2.3")
        second_archive, second_manifest, second_files = self.bundle(
            "1.2.4", b"new synthetic installer", old={old_name: first_files[old_name]})
        original_replace = os.replace
        replacements = []

        def replace(source, target):
            self.assertEqual(Path(target), current)
            self.assertEqual(current.resolve(), old_release)
            self.assertTrue(Path(source).is_symlink())
            deploy.verify_tree(Path(source).resolve(), second_manifest["files"])
            self.assertTrue((self.root / "manifests" / (second_manifest["release_id"] + ".json")).is_file())
            replacements.append((source, target))
            return original_replace(source, target)

        with patch.object(deploy.os, "replace", side_effect=replace):
            second = deploy.publish(second_archive, second_manifest, self.root)
        self.assertEqual(len(replacements), 1)
        self.assertEqual(second["previous_release"], first["release_id"])
        self.assertTrue(current.is_symlink())
        self.assertNotEqual(current.resolve(), old_release)
        self.assertEqual((old_release / old_name).read_bytes(), first_files[old_name])
        self.assertEqual({path.relative_to(current.resolve()).as_posix(): path.read_bytes()
                          for path in current.resolve().rglob("*") if path.is_file()}, second_files)
        release_count = len(list((self.root / "releases").iterdir()))
        again = deploy.publish(second_archive, second_manifest, self.root)
        self.assertEqual(again["release_id"], second["release_id"])
        self.assertEqual(again["current"], second["current"])
        self.assertEqual(len(list((self.root / "releases").iterdir())), release_count)
        self.assertEqual(list(self.root.glob(".current-*")), [])

    def test_existing_installer_cannot_change_or_disappear(self):
        self.require_directory_symlinks()
        archive, manifest, files = self.bundle()
        deploy.publish(archive, manifest, self.root)
        previous = (self.root / "current").resolve()
        old_name = "downloads/" + self.installer_name("1.2.3")
        for old in ({}, {old_name: b"changed versioned installer"}):
            with self.subTest(old=old):
                candidate, metadata, _ = self.bundle("1.2.4", b"new installer", old=old)
                with self.assertRaisesRegex(ValueError, "Existing installer would change or disappear"):
                    deploy.publish(candidate, metadata, self.root)
                self.assertEqual((self.root / "current").resolve(), previous)
                self.assertEqual((previous / old_name).read_bytes(), files[old_name])
                self.assertFalse((self.root / "releases" / metadata["release_id"]).exists())

    def test_failed_atomic_switch_preserves_current_cleans_pending_and_allows_retry(self):
        self.require_directory_symlinks()
        archive, manifest, files = self.bundle()
        deploy.publish(archive, manifest, self.root)
        current = self.root / "current"
        previous = current.resolve()
        old_name = "downloads/" + self.installer_name("1.2.3")
        candidate, metadata, _ = self.bundle("1.2.4", b"second installer", old={old_name: files[old_name]})

        def fail_switch(source, target):
            self.assertEqual(Path(target), current)
            self.assertTrue(Path(source).is_symlink())
            self.assertEqual(current.resolve(), previous)
            deploy.verify_tree(Path(source).resolve(), metadata["files"])
            raise PermissionError("Synthetic atomic switch denied")

        with patch.object(deploy.os, "replace", side_effect=fail_switch):
            with self.assertRaisesRegex(PermissionError, "Synthetic atomic switch denied"):
                deploy.publish(candidate, metadata, self.root)
        self.assertEqual(current.resolve(), previous)
        self.assertEqual((previous / old_name).read_bytes(), files[old_name])
        self.assertEqual(list(self.root.glob(".current-*")), [])
        result = deploy.publish(candidate, metadata, self.root)
        self.assertEqual(result["release_id"], metadata["release_id"])
        self.assertEqual(result["previous_release"], manifest["release_id"])
        self.assertNotEqual(current.resolve(), previous)
        self.assertEqual(list(self.root.glob(".current-*")), [])


if __name__ == "__main__":
    unittest.main()
