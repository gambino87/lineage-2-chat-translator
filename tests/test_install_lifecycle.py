"""Install/uninstall orchestration without windows, downloads or real services."""
import json
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import Mock, patch

import installer_main as setup
from installer_runtime import DEFAULT_MODEL


class InstallLifecycleTests(unittest.TestCase):
    def setUp(self):
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
        window._set_busy = Mock()
        window._report = Mock()
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
            calls.attach_mock(runtime.return_value.stop_service, "stop")
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
        runtime.return_value.stop_service.assert_called_once()
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
        runtime.return_value.stop_service.assert_called_once()
        self.assertEqual(window.events.get_nowait(), ("error", "archive busy"))

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
