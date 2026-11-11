"""Setup safety and recovery checks; no real downloads, processes or models."""
import hashlib
from contextlib import contextmanager
from http.client import IncompleteRead
import io
import json
import os
from pathlib import Path
import tempfile
import socket
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import zipfile

import installer_runtime as runtime
import runtime_integrity
from secure_storage import guard_private_tree


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.messages = []
        self.manager = runtime.RuntimeManager(self.root / "data", self.messages.append)
        self.addCleanup(self.manager._release_storage)
        # Only these inert fixture bytes are trusted in runtime lifecycle tests.
        # Real inventory/hash and socket-owner checks have separate native tests.
        self.fixture_files = {"ollama.exe": b"test-only fake executable; never launched"}
        def manifest():
            return {"upstream_executable_sha256": hashlib.sha256(b"fake test runtime").hexdigest(),
                    "files": [{"path": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                              for name, data in self.fixture_files.items()]}
        self.enterContext(patch.object(runtime_integrity, "load_manifest", side_effect=manifest))
        self.enterContext(patch.object(runtime, "load_manifest", side_effect=manifest))
        self.enterContext(patch.object(runtime, "verify_bundled_frontend", return_value=self.root / "frontend.bin"))
        (self.root / "frontend.bin").write_bytes(self.fixture_files["ollama.exe"])
        def protect_fixture():
            if self.manager._storage_guard is None:
                guard = guard_private_tree(self.manager.data_root, create=True,
                                           authorize_existing=lambda path: path == self.root / "data",
                                           allow_locked_ancestors=True)
                guard.__enter__()
                self.manager._storage_guard = guard
        self.enterContext(patch.object(self.manager, "_protect_storage", side_effect=protect_fixture))
        self.session = MagicMock()
        self.session.identity.pid = 123
        self.session.header.return_value = "Bearer " + "a" * 64
        self.enterContext(patch.object(runtime, "get_session", return_value=self.session))
        self.enterContext(patch.object(runtime, "register_session", return_value=self.session))

    def verified_runtime(self):
        self.manager.runtime_dir.mkdir(parents=True)
        self.manager.executable.write_bytes(b"test-only fake executable; never launched")
        self.manager.receipt_path.write_text(json.dumps({
            "version": runtime.OLLAMA_VERSION,
            "archiveSha256": runtime.ARCHIVE_SHA256,
            "executableSha256": runtime._sha256(self.manager.executable),
        }), encoding="utf-8")

    def response(self, data):
        return io.BytesIO(data if isinstance(data, bytes) else json.dumps(data).encode())

    def test_trusted_inventory_reuses_runtime_without_network_or_process(self):
        self.verified_runtime()
        with patch.object(self.manager, "_download_runtime") as download, patch("installer_runtime.subprocess.Popen") as process:
            self.assertEqual(self.manager.ensure_runtime(), self.manager.executable)
        download.assert_not_called()
        process.assert_not_called()

    def test_changed_executable_is_not_trusted_or_overwritten(self):
        self.verified_runtime()
        self.manager.executable.write_bytes(b"changed")
        with patch.object(self.manager, "_download_runtime") as download:
            with self.assertRaisesRegex(runtime.SetupError, "could not be verified"):
                self.manager.ensure_runtime()
        self.assertEqual(self.manager.executable.read_bytes(), b"changed")
        download.assert_not_called()

    def test_extract_rejects_unsafe_members_before_any_write(self):
        for member in ("../outside.exe", "C:/outside.exe", "\\\\server\\share\\file.exe", "lib/../../outside.exe", "lib\\..\\outside.exe", "normal.txt:stream",
                       "NUL", "lib/CON.txt", "lib/NUL/driver.dll", "lib./driver.dll", "lib /driver.dll"):
            with self.subTest(member=member):
                archive = self.root / "test.zip"
                destination = self.root / "extract"
                destination.mkdir(exist_ok=True)
                with zipfile.ZipFile(archive, "w") as bundle:
                    bundle.writestr("good.txt", "would be safe")
                    bundle.writestr(member, "unsafe")
                with self.assertRaises(runtime.SetupError):
                    runtime._extract_archive(archive, destination)
                self.assertEqual(list(destination.iterdir()), [])

    def test_extract_rejects_symlink(self):
        archive = self.root / "test.zip"
        entry = zipfile.ZipInfo("symlink")
        entry.create_system = 3
        entry.external_attr = 0o120777 << 16
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr(entry, "../outside")
        with self.assertRaises(runtime.SetupError):
            runtime._extract_archive(archive, self.root / "extract")

    def test_verified_archive_is_staged_and_receipted_without_download(self):
        self.fixture_files["lib/ollama/fake.dll"] = b"fake test GPU library"
        self.manager.data_root.mkdir()
        archive = self.manager.data_root / f"ollama-windows-amd64-{runtime.OLLAMA_VERSION}.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("ollama.exe", b"fake test runtime")
            bundle.writestr("lib/ollama/fake.dll", b"fake test GPU library")
        digest = runtime._sha256(archive)
        with patch("installer_runtime.ARCHIVE_SHA256", digest), patch.object(self.manager, "_check_disk"), patch.object(self.manager, "_download_runtime") as download:
            self.manager.ensure_runtime()
            self.assertTrue(self.manager._verified_runtime())
        self.assertEqual((self.manager.runtime_dir / "lib/ollama/fake.dll").read_bytes(), b"fake test GPU library")
        self.assertFalse(archive.exists())
        self.assertFalse(list(self.manager.data_root.glob(".ollama-stage-*")))
        download.assert_not_called()

    def test_checksum_failure_removes_partial_and_installs_nothing(self):
        self.manager.data_root.mkdir()
        archive = self.manager.data_root / "test.zip"
        source = self.response(b"corrupt download")
        source.headers = {"Content-Length": str(len(b"corrupt download"))}
        opener = MagicMock()
        opener.open.return_value = source
        with patch("installer_runtime.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(runtime.SetupError, "SHA-256"):
                self.manager._download_runtime(archive)
        self.assertFalse(archive.exists())
        self.assertFalse(archive.with_suffix(".zip.part").exists())
        self.assertFalse(self.manager.runtime_dir.exists())

    def test_runtime_download_rejects_invalid_length_before_reading_body(self):
        self.manager.data_root.mkdir()
        archive = self.manager.data_root / "test.zip"
        for length in (str(runtime.MAX_RUNTIME_ARCHIVE_BYTES + 1), "-1", "0", "invalid", "9" * 5000):
            with self.subTest(length=length[:30]):
                source = self.response(b"must not read")
                source.headers = {"Content-Length": length}
                source.read1 = MagicMock(side_effect=AssertionError("body should not be read"))
                opener = MagicMock()
                opener.open.return_value = source
                with patch("installer_runtime.request.build_opener", return_value=opener):
                    with self.assertRaisesRegex(runtime.SetupError, "invalid or oversized"):
                        self.manager._download_runtime(archive)
                source.read1.assert_not_called()
                self.assertFalse(archive.exists())
                self.assertFalse(archive.with_suffix(".zip.part").exists())

    def test_runtime_download_bounds_undeclared_or_underdeclared_stream(self):
        self.manager.data_root.mkdir()
        archive = self.manager.data_root / "test.zip"
        for headers in ({}, {"Content-Length": "5"}):
            with self.subTest(headers=headers):
                source = self.response(b"0123456789")
                source.headers = headers
                opener = MagicMock()
                opener.open.return_value = source
                with patch("installer_runtime.request.build_opener", return_value=opener), \
                     patch("installer_runtime.MAX_RUNTIME_ARCHIVE_BYTES", 8):
                    with self.assertRaisesRegex(runtime.SetupError, "exceeded its supported size"):
                        self.manager._download_runtime(archive)
                self.assertFalse(archive.exists())
                self.assertFalse(archive.with_suffix(".zip.part").exists())

    def test_runtime_download_budget_cleans_partial_without_installing(self):
        self.manager.data_root.mkdir()
        archive = self.manager.data_root / "test.zip"
        source = self.response(b"test")
        source.headers = {}
        opener = MagicMock()
        opener.open.return_value = source
        with patch("installer_runtime.request.build_opener", return_value=opener), \
             patch("installer_runtime.RUNTIME_DOWNLOAD_BUDGET_SECONDS", 0):
            with self.assertRaisesRegex(runtime.SetupError, "time budget"):
                self.manager._download_runtime(archive)
        self.assertFalse(archive.exists())
        self.assertFalse(archive.with_suffix(".zip.part").exists())

    def test_bounded_runtime_download_promotes_only_matching_checksum(self):
        self.manager.data_root.mkdir()
        archive = self.manager.data_root / "test.zip"
        body = b"verified fixture bytes"
        source = self.response(body)
        source.headers = {"Content-Length": str(len(body))}
        opener = MagicMock()
        opener.open.return_value = source
        with patch("installer_runtime.request.build_opener", return_value=opener), \
             patch("installer_runtime.MAX_RUNTIME_ARCHIVE_BYTES", len(body)), \
             patch("installer_runtime.ARCHIVE_SHA256", hashlib.sha256(body).hexdigest()):
            self.manager._download_runtime(archive)
        self.assertEqual(archive.read_bytes(), body)
        self.assertFalse(archive.with_suffix(".zip.part").exists())

    def test_successful_cleanup_removes_only_verified_archive_and_preserves_model_partials(self):
        self.verified_runtime()
        archive = self.manager.data_root / f"ollama-windows-amd64-{runtime.OLLAMA_VERSION}.zip"
        archive.write_bytes(b"verified archive fixture")
        partial = self.manager.models_dir / "blobs" / "sha256-test-partial"
        partial.parent.mkdir(parents=True)
        partial.write_bytes(b"resumable model bytes")
        old_stage = self.manager.data_root / ".ollama-stage-previous"
        old_stage.mkdir()
        (old_stage / "unrelated.txt").write_text("not created by this invocation")
        with patch("installer_runtime.ARCHIVE_SHA256", runtime._sha256(archive)), \
             patch.object(self.manager, "_verified_runtime", return_value=True):
            result = self.manager.cleanup_installation()
        self.assertEqual(result, {"removed": [archive.name], "bytes_reclaimed": len(b"verified archive fixture")})
        self.assertFalse(archive.exists())
        self.assertEqual(partial.read_bytes(), b"resumable model bytes")
        self.assertTrue((old_stage / "unrelated.txt").exists())
        self.assertTrue(self.manager.executable.exists())

    def test_cleanup_preserves_unverified_archive_even_with_matching_name(self):
        self.verified_runtime()
        archive = self.manager.data_root / f"ollama-windows-amd64-{runtime.OLLAMA_VERSION}.zip"
        archive.write_bytes(b"user file with coincidentally matching name")
        self.assertEqual(self.manager.cleanup_installation()["removed"], [])
        self.assertTrue(archive.exists())

    def test_cleanup_does_not_remove_archive_without_verified_runtime(self):
        self.manager.data_root.mkdir()
        archive = self.manager.data_root / f"ollama-windows-amd64-{runtime.OLLAMA_VERSION}.zip"
        archive.write_bytes(b"archive fixture")
        with patch("installer_runtime.ARCHIVE_SHA256", runtime._sha256(archive)):
            with self.assertRaisesRegex(runtime.SetupError, "runtime could not be verified"):
                self.manager.cleanup_installation()
        self.assertTrue(archive.exists())

    def test_archive_cleanup_failure_is_reported_and_retry_finishes_without_download(self):
        self.manager.data_root.mkdir()
        archive = self.manager.data_root / f"ollama-windows-amd64-{runtime.OLLAMA_VERSION}.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("ollama.exe", b"fake test runtime")
        digest = runtime._sha256(archive)
        unlink = Path.unlink
        def fail_archive(path, *args, **kwargs):
            if path == archive:
                raise PermissionError("temporary archive locked")
            return unlink(path, *args, **kwargs)
        with patch("installer_runtime.ARCHIVE_SHA256", digest), patch.object(self.manager, "_check_disk"), \
             patch.object(self.manager, "_download_runtime") as download:
            with patch.object(Path, "unlink", fail_archive), self.assertRaisesRegex(runtime.SetupError, "Retry setup to finish cleanup"):
                self.manager.ensure_runtime()
            self.assertTrue(self.manager._verified_runtime())
            self.assertTrue(archive.exists())
            self.assertEqual(self.manager.ensure_runtime(), self.manager.executable)
        self.assertFalse(archive.exists())
        download.assert_not_called()

    def test_connection_failure_before_opening_part_preserves_existing_file(self):
        self.manager.data_root.mkdir()
        archive = self.manager.data_root / "test.zip"
        part = archive.with_suffix(".zip.part")
        part.write_bytes(b"existing unrelated partial")
        opener = MagicMock()
        opener.open.side_effect = runtime.error.URLError("offline")
        with patch("installer_runtime.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(runtime.SetupError, "Could not download"):
                self.manager._download_runtime(archive)
        self.assertEqual(part.read_bytes(), b"existing unrelated partial")

    def test_failed_extract_cleans_only_its_current_staging_folder(self):
        self.manager.data_root.mkdir()
        archive = self.manager.data_root / f"ollama-windows-amd64-{runtime.OLLAMA_VERSION}.zip"
        archive.write_bytes(b"verified fixture")
        other = self.manager.data_root / ".ollama-stage-unrelated"
        other.mkdir()
        (other / "keep.txt").write_text("keep")
        def fail_extract(_archive, stage, _check=None):
            (stage / "partial.txt").write_text("partial extract")
            raise runtime.SetupError("extract failed")
        with patch("installer_runtime.ARCHIVE_SHA256", runtime._sha256(archive)), \
             patch.object(self.manager, "_check_disk"), patch("installer_runtime._extract_archive", side_effect=fail_extract):
            with self.assertRaisesRegex(runtime.SetupError, "extract failed"):
                self.manager.ensure_runtime()
        self.assertEqual(list(self.manager.data_root.glob(".ollama-stage-*")), [other])
        self.assertTrue((other / "keep.txt").exists())
        self.assertTrue(archive.exists())  # A later retry can reuse the verified download.

    def test_staging_cleanup_rejects_reparse_descendant_before_recursive_removal(self):
        self.manager.data_root.mkdir()
        archive = self.manager.data_root / f"ollama-windows-amd64-{runtime.OLLAMA_VERSION}.zip"
        archive.write_bytes(b"verified fixture")
        def fail_extract(_archive, stage, _check=None):
            (stage / "junction").mkdir()
            raise runtime.SetupError("extract failed")
        with patch("installer_runtime.ARCHIVE_SHA256", runtime._sha256(archive)), \
             patch.object(self.manager, "_check_disk"), patch("installer_runtime._extract_archive", side_effect=fail_extract), \
             patch("installer_runtime._is_reparse_path", side_effect=lambda path: path.name == "junction"), \
             patch("installer_runtime.shutil.rmtree") as remove:
            with self.assertRaisesRegex(runtime.SetupError, "staging folder contains a link"):
                self.manager.ensure_runtime()
        remove.assert_not_called()

    def test_cleanup_rejects_reparse_archive_without_removing_anything(self):
        self.verified_runtime()
        archive = self.manager.data_root / f"ollama-windows-amd64-{runtime.OLLAMA_VERSION}.zip"
        archive.write_bytes(b"fixture")
        with patch("installer_runtime._is_reparse_path", side_effect=lambda path: path == archive), \
             patch.object(Path, "unlink") as unlink:
            with self.assertRaisesRegex(runtime.SetupError, "contains a link"):
                self.manager.cleanup_installation()
        unlink.assert_not_called()

    def test_low_disk_blocks_download(self):
        with patch("installer_runtime.shutil.disk_usage", return_value=SimpleNamespace(free=100)), patch.object(self.manager, "_download_runtime") as download:
            with self.assertRaisesRegex(runtime.SetupError, "Choose another storage folder"):
                self.manager.ensure_runtime()
        download.assert_not_called()

    def test_owner_failure_blocks_all_http_requests(self):
        self.manager._opener = MagicMock()
        with patch.object(runtime, "get_session", side_effect=runtime.ServiceIdentityError("different service")):
            with self.assertRaisesRegex(runtime.SetupError, "different service"):
                self.manager._request_api("/api/delete", {"model": runtime.DEFAULT_MODEL}, method="DELETE")
        self.manager._opener.open.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows owner correlation")
    def test_owner_must_match_exact_executable(self):
        connection = MagicMock()
        connection.__enter__.return_value.getsockname.return_value = ("127.0.0.1", 54321)
        owner = {"executable": str(self.root / "other/ollama.exe"), "address": "127.0.0.1", "pid": 10}
        with patch("installer_runtime.socket.create_connection", return_value=connection), patch("installer_runtime.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(owner))) as run:
            with self.assertRaises(runtime.ServiceOwnershipMismatch):
                self.manager._verify_owner()
        args = run.call_args.args[0]
        self.assertIn("54321", args[-1])
        self.assertNotIn(str(self.manager.executable), args[-1])
        self.assertEqual(run.call_args.kwargs["creationflags"], runtime.subprocess.CREATE_NO_WINDOW)

    @unittest.skipUnless(os.name == "nt", "Windows owner correlation")
    def test_incomplete_owner_is_never_classified_as_unrelated(self):
        connection = MagicMock()
        connection.__enter__.return_value.getsockname.return_value = ("127.0.0.1", 54321)
        valid = {"executable": str(self.manager.executable), "address": "127.0.0.1", "pid": 10}
        invalid = [None, [], {}, *({**valid, "executable": value} for value in (None, "", " ", "relative/ollama.exe", 123)),
                   *({**valid, "pid": value} for value in (None, 0, -1, "10", True)),
                   *({**valid, "address": value} for value in (None, "0.0.0.0", "::1"))]
        for owner in invalid:
            with self.subTest(owner=owner), \
                 patch("installer_runtime.socket.create_connection", return_value=connection), \
                 patch("installer_runtime.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(owner))):
                with self.assertRaises(runtime.SetupError) as caught:
                    self.manager._verify_owner()
                self.assertNotIsInstance(caught.exception, runtime.ServiceOwnershipMismatch)
        with patch("installer_runtime.socket.create_connection", return_value=connection), \
             patch("installer_runtime.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(valid))):
            self.assertEqual(self.manager._verify_owner(), valid)

    def test_occupied_service_is_never_stopped_or_replaced(self):
        with patch.object(self.manager, "ensure_runtime") as install, patch.object(self.manager, "_port_open", return_value=True), patch.object(self.manager, "_verify_owner", side_effect=runtime.SetupError("different service")), patch("installer_runtime.subprocess.Popen") as popen:
            with self.assertRaisesRegex(runtime.SetupError, "different service"):
                self.manager.ensure_service()
        popen.assert_not_called()
        install.assert_not_called()

    def test_new_service_has_private_storage_no_cloud_and_hidden_window(self):
        process = MagicMock()
        process.poll.return_value = None
        inherited = {"OLLAMA_DEBUG": "2", "OLLAMA_DEBUG_LOG_REQUESTS": "1", "OLLAMA_ORIGINS": "*",
                     "CUDA_VISIBLE_DEVICES": "1", "HTTPS_PROXY": "https://proxy.example.invalid",
                     "GGML_BACKEND_PATH": "C:/untrusted", "OLLAMA_LLM_LIBRARY": "untrusted",
                     "LLAMA_API_KEY": "inherited-key", "L2CT_TEXT_ONLY": "0",
                     "OLLAMA_CONTEXT_LENGTH": "1024", "PATH": "C:/untrusted"}
        with patch.dict("installer_runtime.os.environ", inherited), \
             patch.object(self.manager, "ensure_runtime"), patch.object(self.manager, "_port_open", side_effect=[False, False, True]), patch.object(self.manager, "_request_api", return_value={"version": runtime.BACKEND_VERSION}), patch("installer_runtime.subprocess.Popen", return_value=process) as popen:
            self.manager.ensure_service()
        kwargs = popen.call_args.kwargs
        self.assertEqual(kwargs["env"]["OLLAMA_NO_CLOUD"], "1")
        self.assertEqual(kwargs["env"]["L2CT_TEXT_ONLY"], "1")
        self.assertEqual(kwargs["env"]["OLLAMA_CONTEXT_LENGTH"], "2048")
        self.assertEqual(kwargs["env"]["OLLAMA_HOST"], "127.0.0.1:11435")
        self.assertEqual(kwargs["env"]["OLLAMA_MODELS"], str(self.manager.models_dir))
        self.assertEqual(kwargs["env"]["OLLAMA_DEBUG"], "0")
        self.assertEqual(kwargs["env"]["OLLAMA_DEBUG_LOG_REQUESTS"], "0")
        self.assertEqual(kwargs["env"]["OLLAMA_ORIGINS"], "")
        self.assertEqual(kwargs["env"]["CUDA_VISIBLE_DEVICES"], "1")
        self.assertEqual(kwargs["env"]["HTTPS_PROXY"], "https://proxy.example.invalid")
        self.assertNotIn("GGML_BACKEND_PATH", kwargs["env"])
        self.assertNotIn("OLLAMA_LLM_LIBRARY", kwargs["env"])
        self.assertNotIn("LLAMA_API_KEY", kwargs["env"])
        self.assertNotIn("untrusted", kwargs["env"]["PATH"])
        self.assertEqual(kwargs["env"]["USERPROFILE"], str(self.manager.service_home))
        self.assertRegex(kwargs["env"]["L2CT_OLLAMA_API_KEY"], r"^[0-9a-f]{64}$")
        self.assertEqual(kwargs["creationflags"], getattr(runtime.subprocess, "CREATE_NO_WINDOW", 0))
        self.assertIs(self.manager._owned_service, process)
        process.terminate.assert_not_called()

    def test_adopted_service_is_not_stopped_by_automatic_cleanup(self):
        with patch.object(self.manager, "ensure_runtime"), \
             patch.object(self.manager, "_verify_owner"), \
             patch.object(self.manager, "_port_open", return_value=True), \
             patch.object(self.manager, "_request_api", return_value={"version": runtime.BACKEND_VERSION}), \
             patch("installer_runtime.subprocess.Popen") as spawn:
            self.manager.ensure_service()
        with patch.object(self.manager, "_port_open") as probe, \
             patch.object(self.manager, "stop_service") as stop:
            self.manager.stop_owned_service()
        self.assertIsNone(self.manager._owned_service)
        spawn.assert_not_called()
        probe.assert_not_called()
        stop.assert_not_called()

    def test_failed_readiness_keeps_child_for_cleanup_before_it_listens(self):
        process = MagicMock(pid=4567)
        process.poll.return_value = None
        with patch.object(self.manager, "ensure_runtime"), \
             patch.object(self.manager, "_port_open", side_effect=[False, False, True]), \
             patch.object(self.manager, "_request_api", side_effect=runtime.SetupError("bad readiness response")), \
             patch("installer_runtime.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(runtime.SetupError, "bad readiness response"):
                self.manager.ensure_service()
        self.assertIs(self.manager._owned_service, process)
        with patch.object(self.manager, "_port_open", return_value=False), \
             patch.object(self.manager, "_verify_owner") as verify, \
             patch.object(self.manager, "_unload_running_models") as unload:
            self.manager.stop_owned_service()
            self.manager.stop_owned_service()
        verify.assert_not_called()
        unload.assert_not_called()
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=runtime.SERVICE_STOP_SECONDS)
        self.assertIsNone(self.manager._owned_service)

    def test_startup_poll_failure_retains_child_and_blocks_another_spawn(self):
        process = MagicMock(pid=4567)
        process.poll.side_effect = OSError("process query failed")
        with patch.object(self.manager, "ensure_runtime"), \
             patch.object(self.manager, "_port_open", return_value=False), \
             patch("installer_runtime.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(OSError, "process query failed"):
                self.manager.ensure_service()
        self.assertIs(self.manager._owned_service, process)
        process.poll.side_effect = None
        process.poll.return_value = None
        with patch.object(self.manager, "_port_open", return_value=False), \
             patch("installer_runtime.subprocess.Popen") as spawn:
            with self.assertRaisesRegex(runtime.SetupError, "previous local service"):
                self.manager.ensure_service()
            self.manager.stop_owned_service()
        spawn.assert_not_called()
        process.terminate.assert_called_once()

    def test_owned_service_unloads_then_stops_using_the_retained_process(self):
        process = MagicMock(pid=4567)
        process.poll.return_value = None
        self.manager._owned_service = process
        calls = MagicMock()
        calls.attach_mock(process.terminate, "terminate")
        calls.attach_mock(process.wait, "wait")
        with patch.object(self.manager, "_port_open", return_value=True), \
             patch.object(self.manager, "_verify_owner", return_value={"pid": 4567}), \
             patch.object(self.manager, "_unload_running_models") as unload, \
             patch.object(self.manager, "_terminate_verified_process") as reopen:
            calls.attach_mock(unload, "unload")
            self.manager.stop_owned_service()
        self.assertEqual([call[0] for call in calls.mock_calls], ["unload", "terminate", "wait"])
        unload.assert_called_once_with(4567)
        reopen.assert_not_called()
        self.assertIsNone(self.manager._owned_service)

    def test_listener_disappearing_during_runtime_check_cannot_replace_owned_handle(self):
        process = MagicMock(pid=4567)
        process.poll.return_value = None
        self.manager._owned_service = process
        with patch.object(self.manager, "_port_open", side_effect=[True, True, False]), \
             patch.object(self.manager, "_verify_owner"), \
             patch.object(self.manager, "ensure_runtime"), \
             patch("installer_runtime.subprocess.Popen") as spawn:
            with self.assertRaisesRegex(runtime.SetupError, "previous local service"):
                self.manager.ensure_service()
        spawn.assert_not_called()
        self.assertIs(self.manager._owned_service, process)

    def test_owned_cleanup_does_not_unload_or_terminate_a_replacement_listener(self):
        process = MagicMock(pid=4567)
        process.poll.return_value = None
        self.manager._owned_service = process
        with patch.object(self.manager, "_port_open", return_value=True), \
             patch.object(self.manager, "_verify_owner", return_value={"pid": 9999}), \
             patch.object(self.manager, "_unload_running_models") as unload, \
             patch.object(self.manager, "_terminate_verified_process") as reopen:
            self.manager.stop_owned_service()
        unload.assert_not_called()
        reopen.assert_not_called()
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=runtime.SERVICE_STOP_SECONDS)

    def test_owned_cleanup_still_stops_child_if_owner_or_unload_checks_fail(self):
        for phase in ("_verify_owner", "_unload_running_models"):
            with self.subTest(phase=phase):
                process = MagicMock(pid=4567)
                process.poll.return_value = None
                self.manager._owned_service = process
                with patch.object(self.manager, "_port_open", return_value=True), \
                     patch.object(self.manager, "_verify_owner", return_value={"pid": 4567}), \
                     patch.object(self.manager, "_unload_running_models"), \
                     patch.object(self.manager, phase, side_effect=runtime.SetupError("not ready")):
                    self.manager.stop_owned_service()
                process.terminate.assert_called_once()
                self.assertIsNone(self.manager._owned_service)

    def test_failed_owned_shutdown_retains_handle_for_retry(self):
        for phase, failure in (("terminate", OSError("stop failed")),
                               ("wait", runtime.subprocess.TimeoutExpired("ollama", 10))):
            with self.subTest(phase=phase):
                process = MagicMock(pid=4567)
                process.poll.return_value = None
                self.manager._owned_service = process
                getattr(process, phase).side_effect = [failure, 0]
                with patch.object(self.manager, "_port_open", return_value=False):
                    with self.assertRaisesRegex(runtime.SetupError, "Retry local service cleanup"):
                        self.manager.stop_owned_service()
                    self.assertIs(self.manager._owned_service, process)
                    self.manager.stop_owned_service()
                self.assertIsNone(self.manager._owned_service)
                self.assertEqual(getattr(process, phase).call_count, 2)

    def test_exited_owned_child_is_forgotten_without_touching_current_listener(self):
        process = MagicMock(pid=4567)
        process.poll.return_value = 0
        self.manager._owned_service = process
        with patch.object(self.manager, "_port_open") as probe:
            self.manager.stop_owned_service()
        probe.assert_not_called()
        process.terminate.assert_not_called()
        process.wait.assert_not_called()
        self.assertIsNone(self.manager._owned_service)

    def test_existing_wrong_version_is_rejected(self):
        with patch.object(self.manager, "ensure_runtime"), patch.object(self.manager, "_verify_owner"), patch.object(self.manager, "_port_open", return_value=True), patch.object(self.manager, "_request_api", return_value={"version": "old"}):
            with self.assertRaisesRegex(runtime.SetupError, "expected"):
                self.manager.ensure_service()

    def test_stop_absent_service_does_not_install_start_or_terminate(self):
        with patch.object(self.manager, "_port_open", return_value=False), patch.object(self.manager, "_verified_runtime") as verified, patch.object(self.manager, "ensure_runtime") as install, patch.object(self.manager, "_terminate_verified_process") as terminate, patch("installer_runtime.subprocess.Popen") as popen:
            self.manager.stop_service()
        verified.assert_not_called()
        install.assert_not_called()
        terminate.assert_not_called()
        popen.assert_not_called()

    def test_stop_foreign_owner_cannot_terminate_any_process(self):
        with patch.object(self.manager, "_port_open", return_value=True), patch.object(self.manager, "_verified_runtime", return_value=True), patch.object(self.manager, "_verify_owner", side_effect=runtime.SetupError("different service")), patch.object(self.manager, "_terminate_verified_process") as terminate:
            with self.assertRaisesRegex(runtime.SetupError, "different service"):
                self.manager.stop_service()
        terminate.assert_not_called()

    def test_stop_requires_fresh_verified_runtime_receipt(self):
        with patch.object(self.manager, "_port_open", return_value=True), patch.object(self.manager, "_verified_runtime", return_value=False), patch.object(self.manager, "_verify_owner") as owner, patch.object(self.manager, "_terminate_verified_process") as terminate:
            with self.assertRaisesRegex(runtime.SetupError, "could not be verified"):
                self.manager.stop_service()
        owner.assert_not_called()
        terminate.assert_not_called()

    def test_stop_targets_only_verified_pid_and_waits_for_port_release(self):
        with patch.object(self.manager, "_port_open", side_effect=[True, True, False]), patch.object(self.manager, "_verified_runtime", return_value=True), patch.object(self.manager, "_verify_owner", return_value={"pid": 4567}), patch.object(self.manager, "_unload_running_models") as unload, patch.object(self.manager, "_terminate_verified_process") as terminate, patch("installer_runtime.time.sleep"):
            self.manager.stop_service()
        unload.assert_called_once_with(4567)
        terminate.assert_called_once_with(4567)
        self.assertIn("stopped", self.messages[-1])

    def test_stop_does_not_kill_another_listener_when_port_stays_occupied(self):
        with patch.object(self.manager, "_port_open", return_value=True), patch.object(self.manager, "_verified_runtime", return_value=True), patch.object(self.manager, "_verify_owner", return_value={"pid": 4567}), patch.object(self.manager, "_unload_running_models"), patch.object(self.manager, "_terminate_verified_process") as terminate, patch("installer_runtime.time.sleep"):
            with self.assertRaisesRegex(runtime.SetupError, "No additional process"):
                self.manager.stop_service()
        terminate.assert_called_once_with(4567)

    def test_stop_rejects_invalid_pid(self):
        with patch.object(self.manager, "_port_open", return_value=True), patch.object(self.manager, "_verified_runtime", return_value=True), patch.object(self.manager, "_verify_owner", return_value={"pid": "4567"}), patch.object(self.manager, "_terminate_verified_process") as terminate:
            with self.assertRaisesRegex(runtime.SetupError, "invalid service process"):
                self.manager.stop_service()
        terminate.assert_not_called()

    def test_shutdown_api_rejects_replacement_pid_before_mutating_it(self):
        self.manager._opener = MagicMock()
        with patch.object(runtime, "get_session", return_value=SimpleNamespace(identity=SimpleNamespace(pid=9999))):
            with self.assertRaisesRegex(runtime.SetupError, "replacement service"):
                self.manager._request_api("/api/generate", {"model": runtime.DEFAULT_MODEL, "keep_alive": 0}, expected_pid=4567)
        self.manager._opener.open.assert_not_called()

    def test_unload_touches_only_distinct_loaded_local_models_without_deleting(self):
        entries = {"models": [
            {"name": runtime.DEFAULT_MODEL}, {"name": runtime.DEFAULT_MODEL},
            {"model": "custom:local"}, {"name": "not:cloud"},
            {"name": "remote:alias", "remote_model": "remote"}, None,
        ]}
        with patch.object(self.manager, "_request_api", side_effect=[entries, {}, {}]) as api:
            self.manager._unload_running_models(4567)
        self.assertEqual([call.args[0] for call in api.call_args_list], ["/api/ps", "/api/generate", "/api/generate"])
        for call, tag in zip(api.call_args_list[1:], (runtime.DEFAULT_MODEL, "custom:local")):
            self.assertEqual(call.args[1], {"model": tag, "prompt": "", "keep_alive": 0, "stream": False})
        for call in api.call_args_list:
            self.assertEqual(call.kwargs["expected_pid"], 4567)
            self.assertGreater(call.kwargs["timeout"], 0)
            self.assertLessEqual(call.kwargs["timeout"], 6)

    def test_unload_budget_stops_before_a_late_generate(self):
        with patch("installer_runtime.time.monotonic", side_effect=[0, 1, 7]), patch.object(self.manager, "_request_api", return_value={"models": [{"name": runtime.DEFAULT_MODEL}]}) as api:
            with self.assertRaisesRegex(runtime.SetupError, "time budget"):
                self.manager._unload_running_models(4567)
        self.assertEqual(len(api.call_args_list), 1)

    def test_unload_rejects_invalid_or_excessive_model_lists(self):
        for entries in ({"models": "bad"}, {"models": [{"name": f"model{number}:local"} for number in range(5)]}):
            with self.subTest(entries=entries), patch.object(self.manager, "_request_api", return_value=entries) as api:
                with self.assertRaises(runtime.SetupError):
                    self.manager._unload_running_models(4567)
                self.assertEqual(len(api.call_args_list), 1)

    def test_unload_failure_does_not_leave_verified_service_running(self):
        with patch.object(self.manager, "_port_open", side_effect=[True, True, False]), patch.object(self.manager, "_verified_runtime", return_value=True), patch.object(self.manager, "_verify_owner", return_value={"pid": 4567}), patch.object(self.manager, "_unload_running_models", side_effect=runtime.SetupError("busy inference")), patch.object(self.manager, "_terminate_verified_process") as terminate:
            self.manager.stop_service()
        terminate.assert_called_once_with(4567)
        self.assertTrue(any("GPU memory release could not be confirmed" in message for message in self.messages))

    def test_unload_response_transport_failure_still_stops_verified_service(self):
        for phase in ("/api/ps", "/api/generate"):
            for failure in (TimeoutError("read timed out"), ConnectionResetError("connection reset"), IncompleteRead(b"{")):
                with self.subTest(phase=phase, failure=type(failure).__name__):
                    self.messages.clear()
                    broken = MagicMock()
                    broken.__enter__.return_value.read.side_effect = failure
                    responses = [broken]
                    if phase == "/api/generate":
                        responses.insert(0, self.response({"models": [{"name": runtime.DEFAULT_MODEL}]}))
                    with patch.object(self.manager, "_port_open", side_effect=[True, True, False]), patch.object(self.manager, "_verified_runtime", return_value=True), patch.object(self.manager, "_verify_owner", return_value={"pid": 4567}), patch.object(self.manager, "_open_api", side_effect=responses) as api, patch.object(self.manager, "_terminate_verified_process") as terminate:
                        self.manager.stop_service()
                    self.assertEqual(api.call_args.args[0], phase)
                    terminate.assert_called_once_with(4567)
                    self.assertTrue(any("GPU memory release could not be confirmed" in message for message in self.messages))

    def test_replaced_owner_after_unload_is_never_terminated(self):
        with patch.object(self.manager, "_port_open", return_value=True), patch.object(self.manager, "_verified_runtime", return_value=True), patch.object(self.manager, "_verify_owner", side_effect=[{"pid": 4567}, {"pid": 9999}]), patch.object(self.manager, "_unload_running_models"), patch.object(self.manager, "_terminate_verified_process") as terminate:
            with self.assertRaisesRegex(runtime.SetupError, "replacement service"):
                self.manager.stop_service()
        terminate.assert_not_called()

    def test_service_exiting_during_unload_is_not_terminated_again(self):
        with patch.object(self.manager, "_port_open", side_effect=[True, False]), patch.object(self.manager, "_verified_runtime", return_value=True), patch.object(self.manager, "_verify_owner", return_value={"pid": 4567}) as owner, patch.object(self.manager, "_unload_running_models"), patch.object(self.manager, "_terminate_verified_process") as terminate:
            self.manager.stop_service()
        self.assertEqual(owner.call_count, 1)
        terminate.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows process handle verification")
    def test_termination_rechecks_image_on_pinned_handle(self):
        kernel = MagicMock()
        kernel.OpenProcess.return_value = 1234
        kernel.WaitForSingleObject.return_value = 0
        def set_image(handle, flags, buffer, length):
            buffer.value = str(self.manager.executable)
            return True
        kernel.QueryFullProcessImageNameW.side_effect = set_image
        with patch("ctypes.WinDLL", return_value=kernel), patch("local_service._user_sid", return_value=b"same-owner"):
            self.manager._terminate_verified_process(4567)
        kernel.TerminateProcess.assert_called_once_with(1234, 0)
        kernel.WaitForSingleObject.assert_called_once_with(1234, 10000)
        self.assertEqual(kernel.CloseHandle.call_count, 2)

    @unittest.skipUnless(os.name == "nt", "Windows process handle verification")
    def test_reused_pid_with_different_image_is_not_terminated(self):
        kernel = MagicMock()
        kernel.OpenProcess.return_value = 1234
        def set_image(handle, flags, buffer, length):
            buffer.value = str(self.root / "unrelated.exe")
            return True
        kernel.QueryFullProcessImageNameW.side_effect = set_image
        with patch("ctypes.WinDLL", return_value=kernel), patch("local_service._user_sid", return_value=b"same-owner"):
            with self.assertRaisesRegex(runtime.SetupError, "process changed"):
                self.manager._terminate_verified_process(4567)
        kernel.TerminateProcess.assert_not_called()
        self.assertEqual(kernel.CloseHandle.call_count, 2)

    @unittest.skipUnless(os.name == "nt", "Windows process account verification")
    def test_matching_executable_under_other_account_is_not_terminated(self):
        kernel = MagicMock()
        kernel.OpenProcess.side_effect = [1234, 5678]
        with patch("ctypes.WinDLL", return_value=kernel), patch("local_service._user_sid", side_effect=[b"other", b"owner"]):
            with self.assertRaises(runtime.ServiceOwnershipMismatch):
                self.manager._terminate_verified_process(4567)
        kernel.TerminateProcess.assert_not_called()
        self.assertEqual(kernel.CloseHandle.call_count, 2)

    def test_model_list_filters_cloud_and_invalid_entries(self):
        entries = {"models": [
            {"name": runtime.DEFAULT_MODEL, "size": 100},
            {"model": "custom:local"},
            {"name": "test:cloud"},
            {"name": "test:local", "remote_host": "example.com"},
            {"name": "bad/remote:model"}, None,
        ]}
        with patch.object(self.manager, "_request_api", return_value=entries):
            self.assertEqual([x["name"] for x in self.manager.installed_models()], [runtime.DEFAULT_MODEL, "custom:local"])

    def test_existing_model_is_checked_without_pulling(self):
        with patch.object(self.manager, "installed_models", return_value=[{"name": runtime.DEFAULT_MODEL}]), patch.object(self.manager, "_verify_local_model") as verify, patch.object(self.manager, "_open_api") as pull:
            self.manager.install_model(runtime.DEFAULT_MODEL)
        verify.assert_called_once_with(runtime.DEFAULT_MODEL)
        pull.assert_not_called()

    def test_streamed_download_requires_success_and_post_install_presence(self):
        stream = b'{"status":"pulling manifest"}\n{"total":100,"completed":100,"digest":"sha256:a"}\n{"status":"success"}\n'
        with patch.object(self.manager, "installed_models", side_effect=[[], [{"name": runtime.DEFAULT_MODEL}]]), patch.object(self.manager, "_check_disk"), patch.object(self.manager, "_open_api", return_value=self.response(stream)), patch.object(self.manager, "_verify_local_model") as verify:
            self.manager.install_model(runtime.DEFAULT_MODEL)
        verify.assert_called_once_with(runtime.DEFAULT_MODEL)
        self.assertTrue(any("100%" in message for message in self.messages))

    def test_incomplete_pull_cannot_report_success(self):
        with patch.object(self.manager, "installed_models", return_value=[]), patch.object(self.manager, "_check_disk"), patch.object(self.manager, "_open_api", return_value=self.response(b'{"status":"pulling manifest"}\n')):
            with self.assertRaisesRegex(runtime.SetupError, "before completion"):
                self.manager.install_model(runtime.DEFAULT_MODEL)

    def test_remote_model_show_is_rejected(self):
        with patch.object(self.manager, "_request_api", return_value={"remote_model": "remote"}):
            with self.assertRaisesRegex(runtime.SetupError, "remote service"):
                self.manager._verify_local_model(runtime.DEFAULT_MODEL)

    def test_remove_only_deletes_explicit_installed_tag(self):
        with patch.object(self.manager, "installed_models", side_effect=[[{"name": runtime.DEFAULT_MODEL}], []]), patch.object(self.manager, "_request_api", return_value={}) as request:
            self.manager.remove_model(runtime.DEFAULT_MODEL)
        request.assert_called_once_with("/api/delete", {"model": runtime.DEFAULT_MODEL}, method="DELETE", timeout=60)

    def test_remove_missing_tag_does_not_mutate_service(self):
        with patch.object(self.manager, "installed_models", return_value=[]), patch.object(self.manager, "_request_api") as request:
            with self.assertRaisesRegex(runtime.SetupError, "not installed"):
                self.manager.remove_model(runtime.DEFAULT_MODEL)
        request.assert_not_called()

    def test_unsupported_model_is_rejected_before_setup_work(self):
        with patch.object(self.manager, "ensure_service") as service:
            with self.assertRaises(runtime.SetupError):
                self.manager.prepare("not-supported:local")
        service.assert_not_called()

    def test_catalog_preserves_research_license_and_unvalidated_status(self):
        self.assertIn("Research", runtime.MODEL_CATALOG["qwen2.5:3b"]["license"])
        self.assertFalse(runtime.MODEL_CATALOG["qwen3.5:4b-q4_K_M"]["tested"])
        self.assertTrue(runtime.MODEL_CATALOG[runtime.DEFAULT_MODEL]["recommended"])
        self.assertGreater(runtime.required_disk_bytes(runtime.DEFAULT_MODEL), 9 * runtime.GIB)


class CancellationTests(unittest.TestCase):
    """Only tiny synthetic loopback peers; never the user's Ollama endpoint."""
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.token = threading.Event()
        self.manager = runtime.RuntimeManager(Path(self.directory) / "data", cancel_event=self.token)
        self.manager.data_root.mkdir()
        self.enterContext(patch.object(self.manager, "_verify_owner", return_value={"pid": 123}))
        # Synthetic HTTP peers below run in this process and have no model.
        session = MagicMock()
        session.identity.pid = 123
        session.header.return_value = "Bearer " + "a" * 64
        self.enterContext(patch.object(runtime, "get_session", return_value=session))
        self.enterContext(patch.object(self.manager, "_protect_storage"))

    @contextmanager
    def peer(self, mode):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(3)
        stop, ready = threading.Event(), threading.Event()
        connections = []
        def serve():
            try:
                connection, _ = listener.accept()
                connections.append(connection)
                connection.settimeout(3)
                if mode == "tls":
                    connection.recv(4096)  # ClientHello; deliberately no handshake reply.
                else:
                    request_data = b""
                    while b"\r\n\r\n" not in request_data:
                        block = connection.recv(4096)
                        if not block:
                            return
                        request_data += block
                    if mode == "headers":
                        connection.sendall(b"HTTP/1.1 200 OK\r\nX-Wait: ")
                    else:
                        connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n")
                ready.set()
                while not stop.wait(0.01):
                    if mode == "progress":
                        connection.sendall(b'{"status":"pulling manifest"}\n')
                    elif mode in ("line", "headers"):
                        connection.sendall(b" ")
            except (OSError, TimeoutError):
                pass
            finally:
                for connection in connections:
                    connection.close()
        server = threading.Thread(target=serve, name="synthetic-setup-peer", daemon=True)
        server.start()
        try:
            yield f"http://127.0.0.1:{listener.getsockname()[1]}", ready
        finally:
            stop.set()
            for connection in connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
            listener.close()
            server.join(4)
            self.assertFalse(server.is_alive(), "synthetic server did not finish")

    def interrupted_request(self, mode, action, *, cancel=True, https=False):
        errors = []
        def work():
            try:
                action()
            except Exception as exc:
                errors.append(exc)
        worker = threading.Thread(target=work, name="synthetic-setup-request", daemon=True)
        started = time.monotonic()
        try:
            with self.peer(mode) as (url, ready), patch.object(runtime, "ENDPOINT", url), \
                 patch.object(runtime, "ARCHIVE_URL", url.replace("http:", "https:") if https else url):
                worker.start()
                self.assertTrue(ready.wait(3), "synthetic request never arrived")
                if cancel:
                    self.token.set()
                worker.join(3)
                self.assertFalse(worker.is_alive(), "network operation ignored cancellation/deadline")
        finally:
            worker.join(4)
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], runtime.SetupCancelled if cancel else runtime.SetupError)
        if not cancel:
            self.assertIn("time budget", str(errors[0]))
        self.assertIsNone(self.manager._network)
        self.assertFalse(any(t.name == "setup-network-cancellation" for t in threading.enumerate()))

    def test_cancel_interrupts_idle_body_and_unfinished_headers(self):
        for mode in ("body", "headers"):
            with self.subTest(mode=mode):
                self.token.clear()
                self.interrupted_request(mode, lambda: self.manager._request_api("/synthetic", timeout=30))

    def test_budget_interrupts_headers_even_while_bytes_keep_arriving(self):
        self.interrupted_request("headers", lambda: self.manager._request_api("/synthetic", timeout=0.3), cancel=False)

    def test_model_pull_budget_bounds_progress_and_unterminated_status_line(self):
        for mode in ("progress", "line"):
            with self.subTest(mode=mode), patch.object(runtime, "MODEL_PULL_BUDGET_SECONDS", 0.3), \
                 patch.object(self.manager, "installed_models", return_value=[]), patch.object(self.manager, "_check_disk"):
                self.interrupted_request(mode, lambda: self.manager.install_model(runtime.DEFAULT_MODEL), cancel=False)

    def test_cancel_model_pull_preserves_all_partial_model_bytes(self):
        partial = self.manager.models_dir / "blobs" / "sha256-synthetic-partial"
        partial.parent.mkdir(parents=True)
        partial.write_bytes(b"resumable model bytes")
        with patch.object(self.manager, "installed_models", return_value=[]), patch.object(self.manager, "_check_disk"):
            self.interrupted_request("line", lambda: self.manager.install_model(runtime.DEFAULT_MODEL))
        self.assertEqual(partial.read_bytes(), b"resumable model bytes")

    def test_cancel_runtime_download_closes_socket_and_removes_only_its_unverified_part(self):
        archive = self.manager.data_root / "synthetic.zip"
        unrelated = self.manager.data_root / "keep.bin"
        unrelated.write_bytes(b"unrelated")
        self.interrupted_request("body", lambda: self.manager._download_runtime(archive))
        self.assertFalse(archive.exists())
        self.assertFalse(archive.with_suffix(".zip.part").exists())
        self.assertEqual(unrelated.read_bytes(), b"unrelated")

    def test_cancel_runtime_tls_handshake_is_not_waiting_for_idle_timeout(self):
        archive = self.manager.data_root / "synthetic.zip"
        self.interrupted_request("tls", lambda: self.manager._download_runtime(archive), https=True)

    def test_cancel_interrupts_proxy_connect_headers_before_tls(self):
        errors = []
        with self.peer("headers") as (url, ready):
            port = int(url.rsplit(":", 1)[1])
            def work():
                try:
                    with runtime._NetworkOperation(self.token, 30, "Synthetic tunnel") as operation:
                        connection = runtime._ControlledHTTPSConnection("127.0.0.1", port, operation=operation, timeout=30)
                        try:
                            connection.set_tunnel("example.invalid", 443)
                            connection.request("GET", "/")
                        finally:
                            connection.close()
                except Exception as exc:
                    errors.append(exc)
            worker = threading.Thread(target=work, name="synthetic-connect-request", daemon=True)
            worker.start()
            try:
                self.assertTrue(ready.wait(3))
                self.token.set()
                worker.join(3)
                self.assertFalse(worker.is_alive())
            finally:
                self.token.set()
                worker.join(4)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], runtime.SetupCancelled)

    def test_model_budget_includes_post_pull_verification(self):
        def verify(_tag):
            time.sleep(0.03)
        with patch.object(runtime, "MODEL_PULL_BUDGET_SECONDS", 0.01), \
             patch.object(self.manager, "installed_models", return_value=[{"name": runtime.DEFAULT_MODEL}]), \
             patch.object(self.manager, "_verify_local_model", side_effect=verify):
            with self.assertRaisesRegex(runtime.SetupError, "time budget"):
                self.manager.install_model(runtime.DEFAULT_MODEL)

    def test_cancelled_setup_cannot_start_an_owned_process(self):
        self.token.set()
        with patch.object(self.manager, "ensure_runtime") as download, patch.object(runtime.subprocess, "Popen") as spawn:
            with self.assertRaises(runtime.SetupCancelled):
                self.manager.prepare()
        download.assert_not_called()
        spawn.assert_not_called()

    def test_cancelled_token_does_not_prevent_owned_service_cleanup(self):
        self.token.set()
        process = MagicMock()
        process.poll.return_value = None
        process.pid = 123
        self.manager._owned_service = process
        with patch.object(self.manager, "_port_open", return_value=True), \
             patch.object(self.manager, "_unload_running_models", side_effect=lambda _pid: self.manager.check_cancelled()) as unload:
            self.manager.stop_owned_service()
        unload.assert_called_once_with(123)
        process.terminate.assert_called_once()
        self.assertIsNone(self.manager._owned_service)
        with self.assertRaises(runtime.SetupCancelled):
            self.manager.check_cancelled()

    def test_cancelled_extraction_preserves_verified_archive_and_removes_current_stage(self):
        archive = self.manager.data_root / f"ollama-windows-amd64-{runtime.OLLAMA_VERSION}.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("ollama.exe", b"fixture runtime")
        real_extract = runtime._extract_archive
        def cancelled_extract(source, target, check):
            self.token.set()
            return real_extract(source, target, check)
        with patch.object(runtime, "ARCHIVE_SHA256", runtime._sha256(archive)), \
             patch.object(self.manager, "_check_disk"), patch.object(runtime, "_extract_archive", side_effect=cancelled_extract):
            with self.assertRaises(runtime.SetupCancelled):
                self.manager.ensure_runtime()
        self.assertTrue(archive.exists())
        self.assertFalse(self.manager.runtime_dir.exists())
        self.assertFalse(list(self.manager.data_root.glob(".ollama-stage-*")))

    def test_setup_client_adapter_restores_client_and_applies_shared_budget(self):
        client = MagicMock()
        original = client._request
        with patch.object(self.manager, "_request_api", return_value={"ok": True}) as api:
            with self.manager.cancellable_client(client, timeout=3):
                self.assertEqual(client._request("/api/show", {"model": runtime.DEFAULT_MODEL}, timeout=30), {"ok": True})
                self.assertLessEqual(api.call_args.kwargs["timeout"], 3)
            self.assertIs(client._request, original)
            with self.assertRaises(runtime.SetupError), self.manager.cancellable_client(client, timeout=0):
                client._request("/api/show")
        self.assertIs(client._request, original)

    def test_setup_client_network_reads_are_cancelled(self):
        client = MagicMock()
        original = client._request
        def action():
            with self.manager.cancellable_client(client):
                client._request("/api/generate", {"prompt": "synthetic"})
        self.interrupted_request("body", action)
        self.assertIs(client._request, original)


if __name__ == "__main__":
    unittest.main()
