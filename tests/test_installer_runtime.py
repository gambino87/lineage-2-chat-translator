"""Setup safety and recovery checks; no real downloads, processes or models."""
import hashlib
from http.client import IncompleteRead
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import zipfile

import installer_runtime as runtime


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.messages = []
        self.manager = runtime.RuntimeManager(self.root / "data", self.messages.append)

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

    def test_valid_receipt_reuses_runtime_without_network_or_process(self):
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
        def fail_extract(_archive, stage):
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
        def fail_extract(_archive, stage):
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
        with patch.object(self.manager, "_verify_owner", side_effect=runtime.SetupError("different service")):
            with self.assertRaisesRegex(runtime.SetupError, "different service"):
                self.manager._request_api("/api/delete", {"model": runtime.DEFAULT_MODEL}, method="DELETE")
        self.manager._opener.open.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows owner correlation")
    def test_owner_must_match_exact_executable(self):
        connection = MagicMock()
        connection.__enter__.return_value.getsockname.return_value = ("127.0.0.1", 54321)
        owner = {"executable": str(self.root / "other/ollama.exe"), "address": "127.0.0.1", "pid": 10}
        with patch("installer_runtime.socket.create_connection", return_value=connection), patch("installer_runtime.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(owner))) as run:
            with self.assertRaisesRegex(runtime.SetupError, "different service"):
                self.manager._verify_owner()
        args = run.call_args.args[0]
        self.assertIn("54321", args[-1])
        self.assertNotIn(str(self.manager.executable), args[-1])
        self.assertEqual(run.call_args.kwargs["creationflags"], runtime.subprocess.CREATE_NO_WINDOW)

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
                     "CUDA_VISIBLE_DEVICES": "1", "HTTPS_PROXY": "https://proxy.example.invalid"}
        with patch.dict("installer_runtime.os.environ", inherited), \
             patch.object(self.manager, "ensure_runtime"), patch.object(self.manager, "_port_open", side_effect=[False, False, True]), patch.object(self.manager, "_request_api", return_value={"version": runtime.OLLAMA_VERSION}), patch("installer_runtime.subprocess.Popen", return_value=process) as popen:
            self.manager.ensure_service()
        kwargs = popen.call_args.kwargs
        self.assertEqual(kwargs["env"]["OLLAMA_NO_CLOUD"], "1")
        self.assertEqual(kwargs["env"]["OLLAMA_HOST"], "127.0.0.1:11435")
        self.assertEqual(kwargs["env"]["OLLAMA_MODELS"], str(self.manager.models_dir))
        self.assertEqual(kwargs["env"]["OLLAMA_DEBUG"], "0")
        self.assertEqual(kwargs["env"]["OLLAMA_DEBUG_LOG_REQUESTS"], "0")
        self.assertEqual(kwargs["env"]["OLLAMA_ORIGINS"], "")
        self.assertEqual(kwargs["env"]["CUDA_VISIBLE_DEVICES"], "1")
        self.assertEqual(kwargs["env"]["HTTPS_PROXY"], "https://proxy.example.invalid")
        self.assertEqual(kwargs["creationflags"], getattr(runtime.subprocess, "CREATE_NO_WINDOW", 0))
        process.terminate.assert_not_called()

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
        with patch.object(self.manager, "_verify_owner", return_value={"pid": 9999}):
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
        with patch("ctypes.WinDLL", return_value=kernel):
            self.manager._terminate_verified_process(4567)
        kernel.TerminateProcess.assert_called_once_with(1234, 0)
        kernel.WaitForSingleObject.assert_called_once_with(1234, 10000)
        kernel.CloseHandle.assert_called_once_with(1234)

    @unittest.skipUnless(os.name == "nt", "Windows process handle verification")
    def test_reused_pid_with_different_image_is_not_terminated(self):
        kernel = MagicMock()
        kernel.OpenProcess.return_value = 1234
        def set_image(handle, flags, buffer, length):
            buffer.value = str(self.root / "unrelated.exe")
            return True
        kernel.QueryFullProcessImageNameW.side_effect = set_image
        with patch("ctypes.WinDLL", return_value=kernel):
            with self.assertRaisesRegex(runtime.SetupError, "process changed"):
                self.manager._terminate_verified_process(4567)
        kernel.TerminateProcess.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(1234)

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


if __name__ == "__main__":
    unittest.main()
