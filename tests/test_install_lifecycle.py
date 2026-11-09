"""Install/uninstall orchestration without windows, downloads or real services."""
import json
from contextlib import ExitStack
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import installer_main as setup
from installer_runtime import DEFAULT_MODEL


class InstallLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(setup, "storage_requires_confirmation", return_value=False))
        # Startup side effects belong to updater integration tests. Keep this
        # older lifecycle suite isolated from the real profile and OS notices.
        for name, value in (("update_in_progress", False), ("uninstall_in_progress", False),
                            ("cleanup_update_cache", None), ("consume_update_result", None),
                            ("detached_launch_notice", None)):
            self.enterContext(patch.object(setup, name, return_value=value))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.game = self.root / "Game" / "system"
        self.game.mkdir(parents=True)
        (self.game / "L2.ini").write_text("fixture")

    def window(self, installer_mode=True):
        window = setup.SetupWindow.__new__(setup.SetupWindow)
        window.root = Mock()
        window.settings = {}
        window.data = Mock(get=lambda: str(self.root / "models-data"))
        window.game = Mock(get=lambda: str(self.game))
        window.initial_game = ""
        window.accepted = Mock(get=lambda: True)
        window.selected_tag = lambda: DEFAULT_MODEL
        window.installer_mode = installer_mode
        window.setup_completed = False
        window.launch_requested = False
        window.ready = False
        window.busy = False
        window._closing = False
        window._cancel_event = threading.Event()
        window._cancellable = False
        window._exiting = False
        window._service_managers = []
        window._work_lock = threading.RLock()
        window._set_busy = Mock(side_effect=lambda busy: setattr(window, "busy", busy))
        window._report = Mock()
        window._show_setup = Mock()
        window.status = Mock()
        window.events = queue.Queue()
        return window

    def test_install_tracks_then_verifies_cleans_saves_and_stops_before_ready(self):
        window = self.window()
        calls = Mock()
        with patch.object(setup.threading, "Thread") as worker, \
             patch.object(setup, "RuntimeManager") as runtime, \
             patch.object(setup, "OllamaClient") as model, \
             patch.object(setup, "save_configuration") as save, \
             patch.object(setup, "register_data_root") as register:
            worker.return_value.start.side_effect = lambda: worker.call_args.kwargs["target"]()
            model.return_value.translate.return_value = "Hello! Looking for a healer."
            calls.attach_mock(register, "track")
            calls.attach_mock(runtime.return_value.prepare, "prepare")
            calls.attach_mock(model.return_value.translate, "translate")
            calls.attach_mock(runtime.return_value.cleanup_installation, "cleanup")
            calls.attach_mock(save, "save")
            calls.attach_mock(runtime.return_value.stop_owned_service, "stop")
            window.install()
        self.assertEqual([call[0] for call in calls.mock_calls], ["track", "prepare", "translate", "cleanup", "save", "stop"])
        self.assertEqual(window.events.get_nowait()[0], "ready")

    def test_failed_model_setup_stops_service_and_never_claims_completion(self):
        window = self.window()
        with patch.object(setup.threading, "Thread") as worker, \
             patch.object(setup, "RuntimeManager") as runtime, \
             patch.object(setup, "OllamaClient") as model, \
             patch.object(setup, "save_configuration") as save, \
             patch.object(setup, "register_data_root"):
            worker.return_value.start.side_effect = lambda: worker.call_args.kwargs["target"]()
            runtime.return_value.prepare.side_effect = RuntimeError("download interrupted")
            window.install()
        runtime.return_value.stop_owned_service.assert_called_once()
        runtime.return_value.cleanup_installation.assert_not_called()
        model.assert_not_called()
        save.assert_not_called()
        self.assertEqual(window.events.get_nowait(), ("error", "download interrupted"))

    def test_temporary_cleanup_failure_is_retryable_and_does_not_save_ready_config(self):
        window = self.window()
        with patch.object(setup.threading, "Thread") as worker, \
             patch.object(setup, "RuntimeManager") as runtime, \
             patch.object(setup, "OllamaClient"), \
             patch.object(setup, "save_configuration") as save, \
             patch.object(setup, "register_data_root"):
            worker.return_value.start.side_effect = lambda: worker.call_args.kwargs["target"]()
            runtime.return_value.cleanup_installation.side_effect = OSError("archive busy")
            window.install()
        save.assert_not_called()
        runtime.return_value.stop_owned_service.assert_called_once()
        self.assertEqual(window.events.get_nowait(), ("error", "archive busy"))

    def configure_operation(self, stack):
        """Run a service worker synchronously; never reach a real service or UI."""
        worker = stack.enter_context(patch.object(setup.threading, "Thread"))
        worker.return_value.start.side_effect = lambda: worker.call_args.kwargs["target"]()
        runtime = stack.enter_context(patch.object(setup, "RuntimeManager"))
        client = stack.enter_context(patch.object(setup, "OllamaClient"))
        client.return_value.translate.return_value = "Synthetic translated sample."
        stack.enter_context(patch.object(setup, "register_data_root"))
        stack.enter_context(patch.object(setup, "save_configuration"))
        return runtime, client

    def test_standalone_success_releases_owned_service_before_ready_and_close(self):
        window = self.window(installer_mode=False)
        with ExitStack() as stack:
            runtime, _ = self.configure_operation(stack)
            window.install()
            runtime.return_value.stop_owned_service.assert_called_once()
            runtime.return_value.stop_service.assert_not_called()
            window._drain()
            self.assertTrue(window.ready)
            window.close()
        window.root.destroy.assert_called_once()
        self.assertEqual(window._service_managers, [])

    def test_standalone_failures_release_owned_service_and_never_offer_handoff(self):
        for stage in ("prepare", "translation", "cleanup", "save"):
            with self.subTest(stage=stage), ExitStack() as stack:
                window = self.window(installer_mode=False)
                runtime, client = self.configure_operation(stack)
                action = {"prepare": runtime.return_value.prepare,
                          "translation": client.return_value.translate,
                          "cleanup": runtime.return_value.cleanup_installation,
                          "save": setup.save_configuration}[stage]
                action.side_effect = RuntimeError("synthetic " + stage + " failure")
                window.install()
                runtime.return_value.stop_owned_service.assert_called_once()
                window._drain()
                self.assertFalse(window.ready)
                self.assertFalse(window.launch_requested)
                window.close()
                window.root.destroy.assert_called_once()

    def test_failed_cleanup_is_retained_and_close_retries_original_manager(self):
        window = self.window(installer_mode=False)
        with ExitStack() as stack:
            runtime, _ = self.configure_operation(stack)
            manager = runtime.return_value
            manager.prepare.side_effect = RuntimeError("download interrupted")
            manager.stop_owned_service.side_effect = [RuntimeError("busy child"), RuntimeError("still busy"), None]
            window.install()
            window._drain()
            self.assertIn("download interrupted", window.status.set.call_args.args[0])
            self.assertIn("busy child", window.status.set.call_args.args[0])
            self.assertEqual(window._service_managers, [manager])
            window.data.get = lambda: str(self.root / "different-folder")
            window.close()
            self.assertTrue(window.busy)
            window.close()  # Repeated X while stopping must not enqueue another stop.
            window._drain()
            self.assertFalse(window.busy)
            window.root.destroy.assert_not_called()
            self.assertIn("Close this window again", window.status.set.call_args.args[0])
            window.close()
            window._drain()
            runtime.assert_called_once()  # Never reinterpret the edited data folder.
            self.assertEqual(manager.stop_owned_service.call_count, 3)
        window.root.destroy.assert_called_once()
        self.assertEqual(window._service_managers, [])

    def test_model_removal_success_and_failure_release_their_owned_service(self):
        for fail in (False, True):
            with self.subTest(fail=fail), ExitStack() as stack:
                window = self.window(installer_mode=False)
                runtime, _ = self.configure_operation(stack)
                stack.enter_context(patch.object(setup, "read_json", return_value={"model": "qwen2.5:3b"}))
                stack.enter_context(patch.object(setup.messagebox, "askyesno", return_value=True))
                if fail:
                    runtime.return_value.remove_model.side_effect = RuntimeError("removal interrupted")
                window.remove()
                runtime.return_value.ensure_service.assert_called_once()
                runtime.return_value.stop_owned_service.assert_called_once()
                self.assertEqual(window.events.get_nowait()[0], "error" if fail else "removed")

    def saved_launch(self, stack):
        stack.enter_context(patch.object(setup, "read_json", side_effect=lambda path:
            {"data_root": str(self.root / "models-data")} if path == setup.RUNTIME_PATH else
            {"model": DEFAULT_MODEL, "log_path": str(self.game / "Network.log")}))

    def test_failed_launch_checks_release_owned_service(self):
        for stage in ("start", "missing model", "verification"):
            with self.subTest(stage=stage), ExitStack() as stack:
                window = self.window(installer_mode=False)
                runtime, client = self.configure_operation(stack)
                self.saved_launch(stack)
                runtime.return_value.installed_models.return_value = [{"name": DEFAULT_MODEL}]
                if stage == "start":
                    runtime.return_value.ensure_service.side_effect = RuntimeError("startup interrupted")
                elif stage == "missing model":
                    runtime.return_value.installed_models.return_value = []
                else:
                    client.return_value._verify_local_model.side_effect = RuntimeError("verification failed")
                window.launch_existing()
                runtime.return_value.stop_owned_service.assert_called_once()
                self.assertEqual(window.events.get_nowait()[0], "error")
                window.root.destroy.assert_not_called()

    def test_open_translator_restarts_checks_and_retains_ownership_through_handoff(self):
        window = self.window(installer_mode=False)
        window.ready = True
        with ExitStack() as stack:
            runtime, client = self.configure_operation(stack)
            self.saved_launch(stack)
            runtime.return_value.installed_models.return_value = [{"name": DEFAULT_MODEL}]
            window.open_translator()
            runtime.return_value.ensure_service.assert_called_once()
            client.return_value._verify_local_model.assert_called_once()
            runtime.return_value.stop_owned_service.assert_not_called()
            window.root.destroy.assert_not_called()
            window._drain()
            self.assertTrue(window.launch_requested)
            window.root.destroy.assert_called_once()
            window.finish()
            window.finish()  # Main's fallback is idempotent after ordinary shutdown.
            runtime.return_value.stop_owned_service.assert_called_once()

    def test_main_releases_owned_child_after_handoff_or_pre_handoff_failure(self):
        for stage in ("normal", "configuration", "import", "translator"):
            with self.subTest(stage=stage), ExitStack() as stack:
                window = self.window(installer_mode=False)
                window.launch_requested = True
                manager = Mock()
                window._service_managers = [manager]
                stack.enter_context(patch.object(setup, "SetupWindow", return_value=window))
                stack.enter_context(patch.object(setup.tk, "Tk"))
                stack.enter_context(patch.object(setup, "apply_window_icon"))
                stack.enter_context(patch.object(setup.messagebox, "showerror"))
                mutex = stack.enter_context(patch.object(setup, "SingleInstance"))
                stack.enter_context(patch.object(setup, "read_json", return_value={} if stage == "configuration"
                                                else {"data_root": str(self.root / "models-data")}))
                if stage == "import":
                    stack.enter_context(patch.dict("sys.modules", {"translator_app": None}))
                else:
                    def launch(**kwargs):
                        manager.stop_owned_service.assert_not_called()
                        if stage == "translator":
                            raise RuntimeError("translator startup failed")
                    stack.enter_context(patch("translator_app.main", side_effect=launch))
                    stack.enter_context(patch("translator_app.SETTINGS", setup.SETTINGS_PATH))
                manager.stop_owned_service.side_effect = lambda: mutex.return_value.close.assert_not_called()
                self.assertEqual(setup.main(["--configure"]), 0 if stage == "normal" else 1)
                manager.stop_owned_service.assert_called_once()
                mutex.return_value.close.assert_called_once()

    def test_finish_prevents_a_queued_worker_from_starting_a_service(self):
        window = self.window(installer_mode=False)
        with patch.object(setup.threading, "Thread") as worker, patch.object(setup, "RuntimeManager") as runtime:
            window.install()
            pending = worker.call_args.kwargs["target"]
            window.finish()
            pending()
            runtime.assert_not_called()

    def test_retry_cannot_start_another_runtime_until_previous_child_is_stopped(self):
        for route in ("install", "remove", "launch_existing"):
            with self.subTest(route=route), ExitStack() as stack:
                window = self.window(installer_mode=False)
                previous = Mock()
                previous.stop_owned_service.side_effect = RuntimeError("previous child is still busy")
                window._service_managers = [previous]
                runtime, _ = self.configure_operation(stack)
                self.saved_launch(stack)
                if route == "remove":
                    stack.enter_context(patch.object(setup, "read_json", return_value={"model": "qwen2.5:3b"}))
                    stack.enter_context(patch.object(setup.messagebox, "askyesno", return_value=True))
                getattr(window, route)()
                runtime.assert_not_called()
                previous.stop_owned_service.assert_called_once()
                self.assertEqual(window._service_managers, [previous])
                self.assertIn("previous child", window.events.get_nowait()[1])

    def test_finish_waits_for_active_work_before_cleaning_its_child(self):
        window = self.window(installer_mode=False)
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        manager = Mock()
        def work():
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release worker")
            window._service_managers.append(manager)
        window._start_work(work)
        self.assertTrue(entered.wait(5))
        closer = threading.Thread(target=lambda: (window.finish(), finished.set()))
        closer.start()
        try:
            self.assertFalse(finished.wait(0.05))
            manager.stop_owned_service.assert_not_called()
        finally:
            release.set()
            closer.join(5)
        self.assertTrue(finished.is_set())
        manager.stop_owned_service.assert_called_once()

    def test_close_during_setup_signals_cancel_and_waits_for_worker_cleanup(self):
        window = self.window(installer_mode=False)
        window.busy = True
        window._cancellable = True
        manager = Mock()
        window._service_managers = [manager]
        with patch.object(setup.messagebox, "showinfo") as info:
            window.close()
        info.assert_not_called()
        self.assertTrue(window._cancel_event.is_set())
        self.assertTrue(window._closing)
        window.root.destroy.assert_not_called()
        manager.stop_owned_service.assert_not_called()

    def test_cancel_before_worker_starts_skips_download_and_retry_has_a_fresh_token(self):
        window = self.window(installer_mode=False)
        with ExitStack() as stack:
            runtime, _ = self.configure_operation(stack)
            setup.threading.Thread.return_value.start.side_effect = None
            window.install()
            pending = setup.threading.Thread.call_args.kwargs["target"]
            cancelled_token = window._cancel_event
            window.cancel_work()
            pending()
            window._drain()
            runtime.assert_not_called()
            self.assertFalse(window.busy)
            self.assertIn("cancelled", window.status.set.call_args.args[0])
            window.root.destroy.assert_not_called()
            setup.threading.Thread.return_value.start.side_effect = lambda: setup.threading.Thread.call_args.kwargs["target"]()
            window.install()
            retry_token = runtime.call_args.kwargs["cancel_event"]
            self.assertIsNot(retry_token, cancelled_token)
            self.assertTrue(cancelled_token.is_set())
            self.assertFalse(retry_token.is_set())
            window._drain()
            self.assertTrue(window.ready)

    def test_cancel_or_close_during_prepare_stops_owned_child_without_saving(self):
        for action in ("cancel_work", "close"):
            for installer_mode in (False, True):
                with self.subTest(action=action, installer_mode=installer_mode), ExitStack() as stack:
                    window = self.window(installer_mode=installer_mode)
                    runtime, client = self.configure_operation(stack)
                    runtime.return_value.prepare.side_effect = lambda _model: getattr(window, action)()
                    window.install()
                    client.assert_not_called()
                    setup.save_configuration.assert_not_called()
                    runtime.return_value.stop_owned_service.assert_called_once()
                    window._drain()
                    self.assertFalse(window.ready)
                    self.assertFalse(window.setup_completed)
                    self.assertFalse(window.launch_requested)
                    self.assertEqual(window.root.destroy.call_count, int(action == "close"))

    def test_cancelled_sample_uses_controlled_transport_and_cannot_save_preferences(self):
        window = self.window(installer_mode=False)
        with ExitStack() as stack:
            runtime, client = self.configure_operation(stack)
            def sample(_text):
                runtime.return_value.cancellable_client.return_value.__enter__.assert_called_once()
                window.cancel_work()
                return "late sample result"
            client.return_value.translate.side_effect = sample
            window.install()
            runtime.return_value.cancellable_client.assert_called_once_with(client.return_value)
            runtime.return_value.cancellable_client.return_value.__exit__.assert_called_once()
            setup.save_configuration.assert_not_called()
            runtime.return_value.stop_owned_service.assert_called_once()
            window._drain()
            self.assertFalse(window.ready)
            self.assertIn("cancelled", window.status.set.call_args.args[0])

    def test_cancel_after_model_cleanup_but_before_commit_does_not_save_configuration(self):
        window = self.window(installer_mode=False)
        with ExitStack() as stack:
            runtime, _ = self.configure_operation(stack)
            runtime.return_value.cleanup_installation.side_effect = window.cancel_work
            window.install()
            setup.save_configuration.assert_not_called()
            runtime.return_value.stop_owned_service.assert_called_once()
            self.assertEqual(window.events.get_nowait()[0], "cancelled")

    def test_cancel_wins_over_queued_ready_including_installer_completion(self):
        for action in ("cancel_work", "close"):
            with self.subTest(action=action), ExitStack() as stack:
                window = self.window(installer_mode=True)
                self.configure_operation(stack)
                window.install()  # Success is queued, but the UI has not consumed it.
                getattr(window, action)()
                window._drain()
                self.assertFalse(window.ready)
                self.assertFalse(window.busy)
                self.assertFalse(window.setup_completed)
                self.assertFalse(window.launch_requested)
                self.assertEqual(window.root.destroy.call_count, int(action == "close"))

    def test_cancel_cleanup_failure_keeps_window_open_and_retry_keeps_original_token(self):
        window = self.window(installer_mode=False)
        with ExitStack() as stack:
            runtime, _ = self.configure_operation(stack)
            manager = runtime.return_value
            manager.prepare.side_effect = lambda _model: window.close()
            manager.stop_owned_service.side_effect = [RuntimeError("child not stopped"), None]
            window.install()
            token = runtime.call_args.kwargs["cancel_event"]
            window._drain()
            window.root.destroy.assert_not_called()
            self.assertFalse(window.busy)
            self.assertFalse(window._closing)
            self.assertIn("child not stopped", window.status.set.call_args.args[0])
            window.close()
            window._drain()
            self.assertTrue(token.is_set())
            self.assertIs(window._cancel_event, token)
            runtime.assert_called_once()
            self.assertEqual(manager.stop_owned_service.call_count, 2)
            window.root.destroy.assert_called_once()

    def test_finish_signals_cancellation_before_waiting_for_active_worker(self):
        window = self.window(installer_mode=False)
        window.busy = True
        entered, observed = threading.Event(), threading.Event()
        manager = Mock()
        def work(cancel_event):
            window._service_managers.append(manager)
            entered.set()
            if cancel_event.wait(5):
                observed.set()
            window._operation_result("ready", "late success", cancel_event=cancel_event)
        window._start_work(work, cleanup_first=True)
        self.assertTrue(entered.wait(5))
        window.finish()
        self.assertTrue(observed.is_set())
        manager.stop_owned_service.assert_called_once()
        self.assertEqual(window.events.get_nowait()[0], "cancelled")

    def test_late_progress_cannot_hide_cancelling_status(self):
        window = self.window(installer_mode=False)
        window.busy = window._cancellable = True
        window.output = Mock()
        window.cancel_work()
        status = window.status.set.call_args.args[0]
        window.events.put(("progress", "Download 50%"))
        window._drain()
        self.assertEqual(window.status.set.call_args.args[0], status)
        window.output.insert.assert_called_once()

    def test_installer_ready_returns_to_wizard_without_launching_translator(self):
        window = self.window()
        window.events.put(("ready", "complete"))
        window._drain()
        self.assertTrue(window.setup_completed)
        self.assertFalse(window.launch_requested)
        window.root.destroy.assert_called_once()
        window.root.after.assert_not_called()

    def test_installer_mode_cannot_start_model_removal_maintenance(self):
        window = self.window()
        with patch.object(setup, "RuntimeManager") as runtime, \
             patch.object(setup.threading, "Thread") as worker:
            window.remove()
        worker.assert_not_called()
        runtime.assert_not_called()

    def test_uninstall_guard_blocks_new_launch_after_instance_lock_is_acquired(self):
        with patch.object(setup, "SingleInstance") as lock, \
             patch.object(setup, "uninstall_in_progress", return_value=True) as guard, \
             patch.object(setup.tk, "Tk"), patch.object(setup.messagebox, "showinfo"), \
             patch.object(setup, "SetupWindow") as window:
            guard.side_effect = lambda: (lock.return_value.acquire.assert_called_once() is None)
            self.assertEqual(setup.main([]), 2)
        window.assert_not_called()
        lock.return_value.close.assert_called_once()

    def test_install_mode_returns_nonzero_for_cancel_and_zero_only_for_completion(self):
        for completed, expected in ((False, 2), (True, 0)):
            with self.subTest(completed=completed), \
                 patch.object(setup, "SingleInstance"), \
                 patch.object(setup.tk, "Tk"), \
                 patch.object(setup, "SetupWindow") as window, \
                 patch("translator_app.main") as launch:
                window.return_value.setup_completed = completed
                window.return_value.launch_requested = True
                self.assertEqual(setup.main(["--install-model"]), expected)
                self.assertEqual(window.call_args.kwargs, {"automatic": False, "installer_mode": True})
                launch.assert_not_called()

    def test_uninstall_cleanup_is_headless_and_releases_instance_lock(self):
        result = self.root / "result.txt"
        with patch.object(setup, "SingleInstance") as lock, \
             patch.object(setup, "uninstall_data", return_value={"ok": True}) as cleanup, \
             patch.object(setup.tk, "Tk", side_effect=AssertionError("No window allowed")):
            self.assertEqual(setup.main(["--uninstall-cleanup", "--cleanup-result", str(result)]), 0)
        cleanup.assert_called_once_with(setup.SETTINGS_PATH.parent)
        lock.return_value.close.assert_called_once()
        self.assertIn("completed", result.read_text(encoding="utf-8-sig"))

    def test_uninstall_reports_retained_unconfirmed_history_by_exit_code(self):
        result = self.root / "result.txt"
        retained = self.root / "unconfirmed-storage-records.json"
        report = {"ok": True, "unconfirmed_records_preserved": [str(retained)],
                  "unconfirmed_preferences_preserved": []}
        with patch.object(setup, "SingleInstance"), \
             patch.object(setup, "uninstall_data", return_value=report), \
             patch.object(setup.tk, "Tk", side_effect=AssertionError("No window allowed")):
            self.assertEqual(setup.main(["--uninstall-cleanup", "--cleanup-result", str(result)]),
                             setup.UNINSTALL_REVIEW_EXIT_CODE)
        self.assertIn(str(retained), result.read_text(encoding="utf-8-sig"))

    def test_old_in_process_cleanup_route_refuses_without_touching_installation(self):
        result = self.root / "program-result.txt"
        with patch.object(setup, "SingleInstance") as lock, \
             patch.object(setup, "read_cleanup_plan") as read, \
             patch.object(setup, "uninstall_data") as uninstall, \
             patch.object(setup, "RuntimeManager") as runtime, \
             patch.object(setup.tk, "Tk", side_effect=AssertionError("No window allowed")):
            self.assertEqual(setup.main(["--cleanup-program-files", "--cleanup-result", str(result)]), 1)
        for operation in (lock, read, uninstall, runtime):
            operation.assert_not_called()
        self.assertIn("rerun the installer", result.read_text(encoding="utf-8-sig"))

    def test_installer_runs_retired_cleanup_before_manual_setup_and_for_silent_updates(self):
        script = (Path(setup.__file__).parent / "packaging/installer.iss").read_text(encoding="utf-8")
        event = script.split("procedure CurStepChanged", 1)[1].split("procedure CurPageChanged", 1)[0]
        self.assertLess(event.index("ProgramCleanupComplete := CleanRetiredProgramFiles"), event.index("and not WizardSilent"))
        self.assertIn("if not ProgramCleanupComplete then Exit", event)
        self.assertIn("#include AppFileList", script)
        self.assertNotIn("recursesubdirs", script)
        cleanup = script.split("function CleanRetiredProgramFiles", 1)[1].split("function InitializeSetup", 1)[0]
        self.assertIn("ExtractTemporaryFile('l2-program-cleanup.ps1')", cleanup)
        self.assertIn("Exec(PowerShellPath", cleanup)
        self.assertIn("ArchitecturesInstallIn64BitMode=x64compatible", script)
        self.assertIn("SW_HIDE, ewWaitUntilTerminated", cleanup)
        self.assertIn("ExpandConstant('{tmp}')", cleanup)
        self.assertIn("DeleteFile(HelperPath)", cleanup)
        self.assertNotIn("Lineage2ChatTranslator.exe", cleanup)
        self.assertNotIn("--cleanup-program-files", cleanup)
        initialize = script.split("function InitializeSetup", 1)[1].split("function InitializeUninstall", 1)[0]
        self.assertIn("if not SetDllDirectory('')", initialize)
        self.assertIn("SetDllDirectoryW@kernel32.dll", script)

    def test_install_security_gate_covers_new_installs_and_existing_updates(self):
        script = (Path(setup.__file__).parent / "packaging/installer.iss").read_text(encoding="utf-8")
        self.assertIn("DefaultDirName={autopf}\\Lineage 2 Chat Translator", script)
        self.assertIn("PrivilegesRequired=lowest", script)
        # Existing updater handoffs explicitly target their registered directory.
        self.assertIn("UsePreviousAppDir=yes", script)
        # Precreated private roots must still be removed when empty on uninstall.
        dirs = script.split("[Dirs]", 1)[1].split("[Files]", 1)[0]
        self.assertIn('Name: "{app}"; Flags: uninsalwaysuninstall', dirs)
        self.assertNotIn("[UninstallDelete]", script)
        preinstall = script.split("function PrepareToInstall", 1)[1].split("function ModelIsReady", 1)[0]
        self.assertIn("if not ProtectInstallationDirectory(False) then", preinstall)
        self.assertNotIn("Exec(", preinstall)
        event = script.split("procedure CurStepChanged", 1)[1].split("procedure CurPageChanged", 1)[0]
        self.assertLess(event.index("ProtectInstallationDirectory(True)"), event.index("CleanRetiredProgramFiles"))
        self.assertIn("if not ProgramSecurityComplete then begin", event)
        failure_branch = event.split("if not ProgramSecurityComplete then begin", 1)[1].split("end;", 1)[0]
        self.assertIn("Exit;", failure_branch)
        run_gate = script.split("function ModelIsReady", 1)[1].split("procedure CurStepChanged", 1)[0]
        self.assertIn("ProgramSecurityComplete and ModelSetupComplete", run_gate)
        helper = script.split("function ProtectInstallationDirectory", 1)[1].split("function CleanRetiredProgramFiles", 1)[0]
        self.assertIn("ExtractTemporaryFile('l2-protect-install-directory.ps1')", helper)
        self.assertIn("ExpandConstant('{sys}\\WindowsPowerShell\\v1.0\\powershell.exe')", helper)
        self.assertIn("SW_HIDE, ewWaitUntilTerminated", helper)
        self.assertNotIn("Lineage2ChatTranslator.exe", helper)

    def test_running_app_or_cleanup_failure_blocks_uninstall_with_a_readable_reason(self):
        for busy in (False, True):
            result = self.root / "result.txt"
            with self.subTest(busy=busy), patch.object(setup, "SingleInstance") as lock, \
                 patch.object(setup, "uninstall_data", side_effect=RuntimeError("model folder busy")) as cleanup, \
                 patch.object(setup.tk, "Tk", side_effect=AssertionError("No window allowed")):
                lock.return_value.acquire.return_value = not busy
                self.assertEqual(setup.main(["--uninstall-cleanup", "--cleanup-result", str(result)]), 1)
                if busy:
                    cleanup.assert_not_called()
                    self.assertIn("Close the translator", result.read_text(encoding="utf-8-sig"))
                else:
                    self.assertIn("model folder busy", result.read_text(encoding="utf-8-sig"))
                lock.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
