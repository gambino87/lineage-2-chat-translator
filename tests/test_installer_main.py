"""Headless installer integration checks; no windows or actual downloads."""
import json
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import app_paths
import installer_main as setup
import translator_app
from installer_runtime import DEFAULT_MODEL


class InstallerConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.settings = self.root / "prefs" / "settings.json"
        self.location = self.root / "prefs" / "runtime.json"
        tracking = patch.object(setup, "register_data_root")
        self.register_data_root = tracking.start()
        self.addCleanup(tracking.stop)
        self.game = self.root / "Game" / "system"
        self.game.mkdir(parents=True)
        (self.game / "L2.ini").write_bytes(b"unchanged game file")

    def install_window(self, game=None, initial_game=""):
        window = setup.SetupWindow.__new__(setup.SetupWindow)
        window.root = Mock()
        window.settings = setup.read_json(self.settings)
        window.data = Mock(get=lambda: str(self.root / "data"))
        window.game = Mock(get=lambda: str(self.game) if game is None else game)
        window.initial_game = initial_game
        window.accepted = Mock(get=lambda: True)
        window.selected_tag = lambda: DEFAULT_MODEL
        window.ready = False
        window.busy = False
        window._closing = False
        window._cancel_event = threading.Event()
        window._cancellable = False
        window._exiting = False
        window._service_managers = []
        window._work_lock = threading.RLock()
        window._set_busy = Mock()
        window._report = Mock()
        window._show_setup = Mock()
        window.events = Mock()
        return window

    def test_configuration_writer_defaults_do_not_copy_personal_preferences(self):
        setup.save_configuration(self.root / "data", DEFAULT_MODEL, "", self.settings, self.location)
        saved = setup.read_json(self.settings)
        self.assertEqual(saved["log_path"], "")
        self.assertEqual(saved["model"], DEFAULT_MODEL)
        self.assertEqual(saved["ollama_url"], "http://127.0.0.1:11435")
        self.assertEqual(saved["overlay"], {})
        self.assertFalse(list(self.settings.parent.glob("*.tmp")))

    def test_upgrade_and_model_switch_preserve_overlay_log_and_language(self):
        old = {"log_path": "D:/Game/system/Network.log", "overlay": {"x": -800, "opacity": 0.7},
               "target_language": "Portuguese", "show_original": True, "include_notices": True}
        setup.write_json(self.settings, old)
        setup.save_configuration(self.root / "data", "qwen2.5:3b", "", self.settings, self.location)
        saved = setup.read_json(self.settings)
        for key, value in old.items():
            self.assertEqual(saved[key], value)
        self.assertEqual(saved["model"], "qwen2.5:3b")

    def test_corrupt_settings_are_not_overwritten(self):
        self.settings.parent.mkdir()
        self.settings.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Cannot read"):
            setup.save_configuration(self.root / "data", DEFAULT_MODEL, "", self.settings, self.location)
        self.assertEqual(self.settings.read_text(), "{broken")
        self.assertFalse(self.location.exists())

    def test_failed_settings_save_restores_the_previous_runtime_location(self):
        old_location = {"data_root": str(self.root / "old-data")}
        setup.write_json(self.location, old_location)
        writer = setup.write_json
        def fail_settings(path, value):
            if path == self.settings:
                raise PermissionError("Preferences are read-only")
            writer(path, value)
        with patch.object(setup, "write_json", side_effect=fail_settings), self.assertRaises(PermissionError):
            setup.save_configuration(self.root / "new-data", DEFAULT_MODEL, "", self.settings, self.location)
        self.assertEqual(setup.read_json(self.location), old_location)
        self.assertFalse(self.settings.exists())

    def test_folder_selection_only_resolves_log_and_never_writes_game_files(self):
        system = self.root / "Game 玩家" / "system"
        system.mkdir(parents=True)
        marker = system / "L2.ini"
        marker.write_bytes(b"original")
        selected, log = setup.validate_selection(str(self.root / "data"), DEFAULT_MODEL, str(system.parent))
        self.assertEqual(selected, self.root / "data")
        self.assertEqual(log, str(system / "Network.log"))
        self.assertEqual(marker.read_bytes(), b"original")
        self.assertEqual(list(system.iterdir()), [marker])

    def test_invalid_model_root_path_and_game_folder_are_rejected(self):
        for path, model, game in [("", DEFAULT_MODEL, ""), ("relative", DEFAULT_MODEL, ""),
                                  (str(Path(self.root.anchor)), DEFAULT_MODEL, ""),
                                  (str(self.root), "unsupported:cloud", ""),
                                  (str(self.root), DEFAULT_MODEL, str(self.root))]:
            with self.subTest(path=path, model=model, game=game), self.assertRaises(ValueError):
                setup.validate_selection(path, model, game)

    def test_packaged_app_uses_writable_roaming_path_and_keeps_selected_3b(self):
        setup.write_json(self.settings, {"model": "qwen2.5:3b"})
        with patch("sys.frozen", True, create=True), patch.dict("os.environ", {"APPDATA": str(self.root)}):
            self.assertEqual(app_paths.settings_file(), self.root / "Lineage2ChatTranslator" / "settings.json")
            with patch.object(translator_app, "SETTINGS", self.settings):
                self.assertEqual(translator_app.load_settings()["model"], "qwen2.5:3b")
        with patch.object(translator_app, "SETTINGS", self.settings):
            self.assertEqual(translator_app.load_settings(migrate_legacy_models=False)["model"], "qwen2.5:3b")

    def test_renamed_app_reuses_existing_preferences_and_model_location(self):
        legacy = self.root / "L2Prominence" / "ChatTranslator"
        setup.write_json(legacy / "runtime.json", {"data_root": "D:/ExistingModels"})
        with patch.dict("os.environ", {"APPDATA": str(self.root)}):
            self.assertEqual(app_paths.runtime_file(), legacy / "runtime.json")
            self.assertEqual(setup.read_json(app_paths.runtime_file())["data_root"], "D:/ExistingModels")
        self.assertFalse((self.root / "Lineage2ChatTranslator").exists())

    def test_current_preferences_take_precedence_over_legacy_copy(self):
        legacy = self.root / "L2Prominence" / "ChatTranslator"
        current = self.root / "Lineage2ChatTranslator"
        setup.write_json(legacy / "settings.json", {"model": "qwen2.5:3b"})
        setup.write_json(current / "settings.json", {"model": DEFAULT_MODEL})
        with patch.dict("os.environ", {"APPDATA": str(self.root)}), patch("sys.frozen", True, create=True):
            self.assertEqual(app_paths.settings_file(), current / "settings.json")
            self.assertEqual(setup.read_json(app_paths.settings_file())["model"], DEFAULT_MODEL)

    def test_headless_entrypoint_never_creates_window(self):
        output = self.root / "report.json"
        with patch.object(setup.tk, "Tk", side_effect=AssertionError("No windows allowed")), \
             patch.object(setup, "self_test", return_value={"ok": True, "windows_opened": False}):
            self.assertEqual(setup.main(["--self-test", "--output", str(output)]), 0)
        self.assertEqual(setup.read_json(output), {"ok": True, "windows_opened": False})

    @unittest.skipUnless(setup.os.name == "nt", "Windows hardware query")
    def test_hardware_query_uses_system_binary_without_searching_current_folder(self):
        trusted = Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
        with patch.object(setup, "_system_powershell", return_value=trusted), \
             patch.object(setup.subprocess, "run", return_value=Mock(returncode=0, stdout="Test GPU")) as run:
            description = setup.hardware_summary(self.root)
        self.assertEqual(run.call_args.args[0][0], str(trusted))
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertIn("GPU: Test GPU", description)

    @unittest.skipUnless(setup.os.name == "nt", "Windows hardware query")
    def test_missing_system_powershell_skips_optional_gpu_query(self):
        with patch.object(setup, "_system_powershell", side_effect=RuntimeError("unavailable")), \
             patch.object(setup.subprocess, "run") as run:
            description = setup.hardware_summary(self.root)
        run.assert_not_called()
        self.assertIn("Windows", description)

    def test_setup_worker_commits_only_after_successful_runtime_and_sample(self):
        window = self.install_window()
        # Execute worker synchronously while keeping the real orchestration.
        with patch.object(setup.threading, "Thread") as worker, patch.object(setup, "RuntimeManager") as manager, \
             patch.object(setup, "OllamaClient") as client, patch.object(setup, "save_configuration") as save:
            worker.return_value.start.side_effect = lambda: worker.call_args.kwargs["target"]()
            manager.return_value.prepare.side_effect = RuntimeError("Download interrupted")
            window.install()
            save.assert_not_called()
            client.assert_not_called()
            self.assertEqual(window.events.put.call_args.args[0], ("error", "Download interrupted"))
            manager.return_value.prepare.side_effect = None
            client.return_value.translate.return_value = "Hello! Looking for a healer in the party."
            window.install()
            save.assert_called_once_with(self.root / "data", DEFAULT_MODEL, str(self.game / "Network.log"))
            self.assertEqual(window.events.put.call_args.args[0][0], "ready")

    def test_missing_or_invalid_game_folder_is_rejected_before_runtime_work(self):
        for folder in ("", " \t ", str(self.root / "unavailable"), str(self.root)):
            with self.subTest(folder=folder):
                window = self.install_window(folder)
                with patch.object(setup.threading, "Thread") as worker, \
                     patch.object(setup, "RuntimeManager") as manager, \
                     patch.object(setup, "OllamaClient") as client, \
                     patch.object(setup, "save_configuration") as save, \
                     patch.object(setup.messagebox, "showerror") as error:
                    window.install()
                error.assert_called_once()
                worker.assert_not_called()
                manager.assert_not_called()
                client.assert_not_called()
                save.assert_not_called()
                window._set_busy.assert_not_called()

    def test_setup_saves_selected_log_before_offering_to_open_translator(self):
        window = self.install_window(str(self.game.parent))
        save = setup.save_configuration
        with patch.object(setup.threading, "Thread") as worker, \
             patch.object(setup, "RuntimeManager"), patch.object(setup, "OllamaClient"), \
             patch.object(setup, "save_configuration", side_effect=lambda data, model, log: save(data, model, log, self.settings, self.location)):
            worker.return_value.start.side_effect = lambda: worker.call_args.kwargs["target"]()
            window.install()
        self.assertEqual(window.events.put.call_args.args[0][0], "ready")
        self.assertEqual(setup.read_json(self.settings)["log_path"], str(self.game / "Network.log"))
        self.assertFalse((self.game / "Network.log").exists())
        self.assertEqual((self.game / "L2.ini").read_bytes(), b"unchanged game file")
        window.root.destroy.assert_not_called()

    def test_unchanged_folder_preserves_custom_log_but_changed_folder_updates_it(self):
        custom_log = str(self.game / "custom-chat.log")
        for selected, expected in ((str(self.game) + "/.", custom_log), (str(self.root / "OtherGame"), str(self.root / "OtherGame" / "system" / "Network.log"))):
            with self.subTest(selected=selected):
                other = self.root / "OtherGame" / "system"
                other.mkdir(parents=True, exist_ok=True)
                (other / "L2.ini").write_bytes(b"other game file")
                setup.write_json(self.settings, {"log_path": custom_log, "overlay": {"x": 12}, "target_language": "Portuguese"})
                window = self.install_window(selected, initial_game=str(self.game))
                save = setup.save_configuration
                with patch.object(setup.threading, "Thread") as worker, \
                     patch.object(setup, "RuntimeManager"), patch.object(setup, "OllamaClient"), \
                     patch.object(setup, "save_configuration", side_effect=lambda data, model, log: save(data, model, log, self.settings, self.location)):
                    worker.return_value.start.side_effect = lambda: worker.call_args.kwargs["target"]()
                    window.install()
                saved = setup.read_json(self.settings)
                self.assertEqual(saved["log_path"], expected)
                self.assertEqual(saved["overlay"], {"x": 12})
                self.assertEqual(saved["target_language"], "Portuguese")

    def test_existing_runtime_without_log_stays_in_setup_without_starting_service(self):
        setup.write_json(self.location, {"data_root": str(self.root / "data")})
        for log in (None, "", "  ", 42):
            with self.subTest(log=log):
                setup.write_json(self.settings, {"model": DEFAULT_MODEL, "log_path": log})
                window = self.install_window()
                window.status = Mock()
                with patch.object(setup, "SETTINGS_PATH", self.settings), patch.object(setup, "RUNTIME_PATH", self.location), \
                     patch.object(setup.threading, "Thread") as worker, patch.object(setup, "RuntimeManager") as manager:
                    window.launch_existing()
                worker.assert_not_called()
                manager.assert_not_called()
                window._set_busy.assert_not_called()
                self.assertIn("system folder", window.status.set.call_args.args[0])
                window._show_setup.assert_called_once()
                window.root.destroy.assert_not_called()

    def test_constructor_normalizes_invalid_saved_logs_before_folder_initialization(self):
        def variable(value=""):
            result = Mock()
            result.get.return_value = value
            return result
        for log in (None, "", "  \t ", 42, {}):
            with self.subTest(log=log):
                setup.write_json(self.settings, {"model": DEFAULT_MODEL, "log_path": log})
                with patch.object(setup, "SETTINGS_PATH", self.settings), patch.object(setup, "RUNTIME_PATH", self.location), \
                     patch.object(setup.tk, "StringVar", side_effect=variable), patch.object(setup.tk, "BooleanVar", side_effect=variable), \
                     patch.object(setup.SetupWindow, "_build"), patch.object(setup.threading, "Thread") as worker, \
                     patch.object(setup.messagebox, "showerror") as error:
                    window = setup.SetupWindow(Mock(), automatic=True)
                    self.assertEqual(window.game.get(), "")
                    self.assertEqual(window.initial_game, "")
                    worker.reset_mock()
                    window.install()
                    worker.assert_not_called()
                    error.assert_called_once()

    def test_initial_folder_cannot_bypass_validation_without_actual_saved_log(self):
        for log in (None, "", " \t ", 42):
            with self.subTest(log=log):
                setup.write_json(self.settings, {"model": DEFAULT_MODEL, "log_path": log})
                window = self.install_window(str(self.root), initial_game=str(self.root))
                with patch.object(setup.threading, "Thread") as worker, patch.object(setup, "RuntimeManager") as manager, \
                     patch.object(setup.messagebox, "showerror") as error:
                    window.install()
                worker.assert_not_called()
                manager.assert_not_called()
                error.assert_called_once()

    def test_existing_custom_log_can_launch_without_changing_saved_preferences(self):
        old = {"model": DEFAULT_MODEL, "log_path": str(self.game / "custom-chat.log"), "overlay": {"x": 12}}
        setup.write_json(self.settings, old)
        setup.write_json(self.location, {"data_root": str(self.root / "data")})
        window = self.install_window()
        window.status = Mock()
        with patch.object(setup, "SETTINGS_PATH", self.settings), patch.object(setup, "RUNTIME_PATH", self.location), \
             patch.object(setup.threading, "Thread") as worker, patch.object(setup, "RuntimeManager") as manager, \
             patch.object(setup, "OllamaClient"):
            worker.return_value.start.side_effect = lambda: worker.call_args.kwargs["target"]()
            manager.return_value.installed_models.return_value = [{"name": DEFAULT_MODEL}]
            window.launch_existing()
        manager.return_value.ensure_service.assert_called_once()
        self.assertEqual(window.events.put.call_args.args[0], ("launch", ""))
        self.assertEqual(setup.read_json(self.settings), old)

    def test_success_replaces_download_action_with_open_and_hides_progress(self):
        window = setup.SetupWindow.__new__(setup.SetupWindow)
        window.root = Mock()
        window.events = queue.Queue()
        window.events.put(("ready", "Setup complete."))
        window.ready, window.busy = False, True
        window._closing = window._cancellable = False
        window._cancel_event = threading.Event()
        window.model_box, window.install_button, window.open_button = Mock(), Mock(), Mock()
        window.progress, window.status = Mock(), Mock()
        window.controls = [window.model_box, window.install_button]
        window._show_setup = Mock()
        window._drain()
        self.assertTrue(window.ready)
        self.assertFalse(window.busy)
        window.install_button.grid_remove.assert_called_once()
        window.open_button.grid.assert_called_once()
        window.open_button.configure.assert_called_with(state="normal")
        window.progress.stop.assert_called_once()
        window.progress.grid_remove.assert_called_once()

    def test_failed_setup_returns_to_retry_action_without_opening_translator(self):
        window = setup.SetupWindow.__new__(setup.SetupWindow)
        window.root = Mock()
        window.events = queue.Queue()
        window.events.put(("error", "Connection interrupted."))
        window.ready, window.busy = False, True
        window._closing = window._cancellable = False
        window._cancel_event = threading.Event()
        window.model_box, window.install_button, window.open_button = Mock(), Mock(), Mock()
        window.progress, window.status = Mock(), Mock()
        window.controls = [window.model_box, window.install_button]
        window._show_setup = Mock()
        window._drain()
        self.assertFalse(window.ready)
        self.assertFalse(window.busy)
        window.install_button.grid.assert_called_once()
        window.install_button.configure.assert_called_with(state="normal", text="Download & configure")
        window.open_button.grid_remove.assert_called_once()
        window.root.destroy.assert_not_called()
        self.assertIn("Connection interrupted.", window.status.set.call_args.args[0])
        window._show_setup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
