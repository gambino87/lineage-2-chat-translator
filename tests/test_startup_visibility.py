"""Compact startup progress and visible configuration/repair. No GUI or service calls."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import installer_main as setup
from installer_runtime import DEFAULT_MODEL


class StartupVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(setup, "storage_requires_confirmation", return_value=False))
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.settings = Path(folder) / "settings.json"
        self.location = Path(folder) / "runtime.json"
        self.saved = {"model": DEFAULT_MODEL, "log_path": "D:/Game/system/custom-chat.log", "overlay": {"x": 12}}
        setup.write_json(self.settings, self.saved)
        setup.write_json(self.location, {"data_root": str(Path(folder) / "models")})
        self.enterContext(patch.object(setup, "SETTINGS_PATH", self.settings))
        self.enterContext(patch.object(setup, "RUNTIME_PATH", self.location))
        def variable(value="", master=None):
            return Mock(get=Mock(return_value=value))
        self.enterContext(patch.object(setup.tk, "StringVar", side_effect=variable))
        self.enterContext(patch.object(setup.tk, "BooleanVar", side_effect=variable))
        self.enterContext(patch.object(setup, "LocalizedStringVar", side_effect=variable))
        self.build = self.enterContext(patch.object(setup.SetupWindow, "_build"))
        self.start_frame = self.enterContext(patch.object(setup.tk, "Frame"))
        self.start_frame.return_value.winfo_reqwidth.return_value = 440
        self.start_frame.return_value.winfo_reqheight.return_value = 190
        self.start_label = self.enterContext(patch.object(setup.tk, "Label"))
        self.start_progress = self.enterContext(patch.object(setup.ttk, "Progressbar"))
        self.hardware = self.enterContext(patch.object(setup, "hardware_summary", return_value="Test hardware"))
        self.enterContext(patch.object(setup, "register_data_root"))
        self.runtime = self.enterContext(patch.object(setup, "RuntimeManager"))
        self.runtime.return_value.installed_models.return_value = [{"name": DEFAULT_MODEL}]
        self.runtime.return_value.executable.is_file.return_value = True
        self.client = self.enterContext(patch.object(setup, "OllamaClient"))
        self.worker = self.enterContext(patch.object(setup.threading, "Thread"))
        self.worker.return_value.start.side_effect = lambda: self.worker.call_args.kwargs["target"]()

    def window(self, automatic=True, installer_mode=False):
        root = Mock()
        if automatic:
            self.build.side_effect = lambda: root.withdraw.assert_called_once()
        else:
            self.build.side_effect = None
        window = setup.SetupWindow(root, automatic=automatic, installer_mode=installer_mode)
        window._setup_frame = Mock()
        window._set_busy = Mock(side_effect=lambda busy: setattr(window, "busy", busy))
        window.output = Mock()
        return window

    def test_configured_launch_never_reveals_setup_and_skips_hardware_scan(self):
        window = self.window()
        self.runtime.assert_not_called()
        self.hardware.assert_not_called()
        window.launch_existing()
        window._drain()
        window.root.deiconify.assert_called_once()
        window._setup_frame.pack_forget.assert_called_once()
        window._setup_frame.pack.assert_not_called()
        self.start_progress.return_value.start.assert_called_once()
        self.start_progress.return_value.stop.assert_called_once()
        self.start_frame.return_value.destroy.assert_called_once()
        window.root.destroy.assert_called_once()
        self.assertTrue(window.launch_requested)
        self.runtime.return_value.ensure_service.assert_called_once()
        self.client.return_value._verify_local_model.assert_called_once()
        self.hardware.assert_not_called()
        self.assertEqual(setup.read_json(self.settings), self.saved)

    def test_configuration_and_installer_modes_show_setup_immediately(self):
        for installer_mode in (False, True):
            with self.subTest(installer_mode=installer_mode):
                self.hardware.reset_mock()
                window = self.window(automatic=False, installer_mode=installer_mode)
                window.root.withdraw.assert_not_called()
                window.root.deiconify.assert_called_once()
                self.hardware.assert_called_once()
                window._show_setup()
                self.hardware.assert_called_once()  # Revealing again does not rescan.
                self.runtime.assert_not_called()
                self.start_frame.assert_not_called()

    def test_untrusted_legacy_store_requires_visible_owner_review_before_start(self):
        window = self.window()
        window._start_work = Mock()
        with patch.object(setup, "storage_requires_confirmation", return_value=True):
            window.launch_existing()
        self.runtime.assert_not_called()
        self.client.assert_not_called()
        window._start_work.assert_not_called()  # Only the read-only hardware summary may run.
        self.assertFalse(window.launch_requested)
        self.assertIn("Review your existing model folder", window.status.set.call_args.args[0])
        self.assertEqual(setup.read_json(self.settings), self.saved)

    def test_incomplete_configuration_reveals_setup_without_starting_service(self):
        for saved, location in (({}, {}), ({"model": DEFAULT_MODEL}, {"data_root": "D:/Models"}),
                                ({**self.saved, "log_path": " "}, {"data_root": "D:/Models"})):
            with self.subTest(saved=saved):
                setup.write_json(self.settings, saved)
                setup.write_json(self.location, location)
                window = self.window()
                window.launch_existing()
                window.root.deiconify.assert_called_once()
                window.root.destroy.assert_not_called()
                self.assertFalse(window.launch_requested)
                self.runtime.assert_not_called()
                self.start_frame.assert_not_called()

    def test_missing_runtime_reveals_repair_without_hidden_download(self):
        window = self.window()
        self.runtime.return_value.executable.is_file.return_value = False
        window.launch_existing()
        window._drain()
        self.assertEqual(window.root.deiconify.call_count, 2)
        window._setup_frame.pack.assert_called_once_with(fill="both", expand=True)
        self.start_progress.return_value.stop.assert_called_once()
        window.root.resizable.assert_called_with(True, True)
        window.root.geometry.assert_called_with("740x700")
        window.root.destroy.assert_not_called()
        self.assertFalse(window.launch_requested)
        self.runtime.return_value.ensure_runtime.assert_not_called()
        self.runtime.return_value.ensure_service.assert_not_called()
        self.assertIn("runtime is missing", window.status.set.call_args.args[0])

    def test_missing_model_or_service_failure_reveals_setup(self):
        for failure in ("missing model", "service failed", "verification failed"):
            with self.subTest(failure=failure):
                self.runtime.return_value.installed_models.return_value = [] if failure == "missing model" else [{"name": DEFAULT_MODEL}]
                self.runtime.return_value.ensure_service.side_effect = RuntimeError(failure) if failure == "service failed" else None
                self.client.return_value._verify_local_model.side_effect = RuntimeError(failure) if failure == "verification failed" else None
                window = self.window()
                window.launch_existing()
                window._drain()
                self.assertEqual(window.root.deiconify.call_count, 2)
                window._setup_frame.pack.assert_called_once_with(fill="both", expand=True)
                self.assertIsNone(window._startup_frame)
                window.root.destroy.assert_not_called()
                self.assertFalse(window.launch_requested)
                self.assertIn("Setup stopped:", window.status.set.call_args.args[0])

    def test_changed_unreadable_preferences_cannot_leave_startup_hidden(self):
        window = self.window()
        self.settings.write_text("invalid JSON", encoding="utf-8")
        window.launch_existing()
        window.root.deiconify.assert_called_once()
        window.root.destroy.assert_not_called()
        self.runtime.assert_not_called()
        self.assertIn("Could not open", window.status.set.call_args.args[0])

    def test_starting_view_precedes_service_work_and_stays_until_completion(self):
        window = self.window()
        def start_service():
            window.root.deiconify.assert_called_once()
            self.start_progress.return_value.start.assert_called_once()
            self.start_frame.return_value.destroy.assert_not_called()
            window._setup_frame.pack.assert_not_called()
            self.assertTrue(window.busy)
        self.runtime.return_value.ensure_service.side_effect = start_service
        window.launch_existing()
        window.root.destroy.assert_not_called()
        self.start_progress.return_value.stop.assert_not_called()
        self.assertTrue(any(call.kwargs.get("text") == "Starting translator…" for call in self.start_label.call_args_list))
        window.root.geometry.assert_called_with("460x210")
        window.root.resizable.assert_called_with(False, False)
        window._show_starting()
        self.start_frame.assert_called_once()  # No duplicate screen.
        with patch.object(setup.messagebox, "showinfo") as info:
            window.close()
            window.close()
        info.assert_not_called()
        window.root.destroy.assert_not_called()
        window._drain()
        window.root.destroy.assert_called_once()
        self.assertFalse(window.launch_requested)  # Close wins over queued launch.
        self.runtime.return_value.stop_owned_service.assert_called_once()
        self.start_progress.return_value.stop.assert_called_once()
        self.assertIsNone(window._startup_frame)


if __name__ == "__main__":
    unittest.main()
